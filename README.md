# Audio Cleanup — 音频录音自动清理工具

一款基于 AI 的音频清理工具，自动去除录音中的**停顿、口头禅、重复内容和废话段落**，类似剪映的"删口癖"功能，但更强大——支持语义级别的重复检测和 AI 辅助的重录片段识别。

## 功能特性

| 功能 | 说明 |
|------|------|
| 静音/停顿去除 | 自动检测并裁剪超长停顿，保留自然节奏 |
| 呼吸声检测 | 识别并去除句间呼吸声 |
| 口头禅/语气词去除 | 中文：嗯、啊、呃、那个、就是说等；英文：um、uh、you know 等 |
| 文本级重复检测 | 三层检测：句子级、短语级、单词级 |
| AI 辅助重录识别 | 通过外部 LLM 语义分析，识别录音中的"重录片段"（说错后重新说的段落） |
| 精准剪辑 | 基于 FFmpeg 从原始文件精确裁切，避免二次编码损失 |
| 转录缓存 | Whisper 转录结果缓存为 JSON，后续处理无需重新转录 |

## 系统要求

- Python 3.10+
- FFmpeg（用于音频裁切和拼接）
- 约 2GB 磁盘空间（Whisper medium 模型）

## 安装

```bash
# 克隆仓库
git clone https://github.com/sarahmitchell-cpu/audio-cleanup.git
cd audio-cleanup

# 安装依赖
pip install -r requirements.txt

# 确保 FFmpeg 已安装
# macOS: brew install ffmpeg
# Ubuntu: sudo apt install ffmpeg
```

## 快速开始

```bash
# 基本用法：全功能清理（中文）
python3 audio_cleanup.py recording.wav

# 指定输出文件
python3 audio_cleanup.py recording.wav -o cleaned.wav

# 英文音频
python3 audio_cleanup.py recording.wav --language en

# 仅去除停顿
python3 audio_cleanup.py recording.wav --mode silence

# 去除停顿 + 口头禅
python3 audio_cleanup.py recording.wav --mode filler

# 全功能清理（停顿 + 口头禅 + 重复检测）
python3 audio_cleanup.py recording.wav --mode full

# 生成 JSON 报告
python3 audio_cleanup.py recording.wav --report
```

## AI 辅助清理（两阶段流程）

对于重录内容较多的录音（如播客、旁白、课程录制），推荐使用 AI 辅助的两阶段处理流程：

### 阶段一：转录

```bash
# 仅转录，不处理音频（耗时约 25 分钟/30 分钟音频）
python3 audio_cleanup.py recording.wav --transcribe-only
```

生成两个文件：
- `recording_transcript.json` — 逐字级别的转录结果
- `recording_sentences.json` — 按停顿分组的句子列表

### 阶段二：AI 分析 + 应用

将 `recording_sentences.json` 发送给 LLM（如 Claude、GPT-4），请它分析哪些句子是"重录的废片段"（说错了重新说的部分），返回需要删除的句子 ID 列表。

**提示词示例：**
```
以下是一段录音的逐句转录。录音者在录制过程中经常说错后重新说。
请分析每一句，识别出哪些是"重录前的废片段"（即说错后被重新说过的句子）。
返回所有应该删除的句子ID列表（JSON数组格式）。

规则：
- 如果连续多句说的是同一段内容，保留最后一次（最完整的版本），前面的标记为删除
- 开头的试音、清嗓子等也应删除
- 不确定的句子不要删除

[粘贴 sentences.json 内容]
```

将 AI 返回的 ID 列表保存为 JSON 文件（如 `retakes.json`）：
```json
[0, 2, 3, 7, 17, 28, 29, 32, 33, ...]
```

然后应用：
```bash
# 使用 AI 标记的重录ID + 文本检测 + 口头禅去除，一起处理
python3 audio_cleanup.py recording.wav --ai-retakes retakes.json --report
```

## 处理流程详解

### 整体架构

```
输入音频
   │
   ├──[缓存存在?]──→ 加载缓存转录
   │       否
   ▼
┌─────────────────────────┐
│  Whisper 语音转录        │  faster-whisper, word_timestamps=True
│  生成逐字时间戳          │  VAD 过滤，最小静音 300ms
└─────────────────────────┘
   │
   ▼ 保存 transcript.json + sentences.json（缓存）
   │
┌─────────────────────────┐
│  Step 1: 口头禅检测      │  匹配预定义语气词列表
│  (Filler Detection)     │  标记 keep=False
└─────────────────────────┘
   │
┌─────────────────────────┐
│  Step 2: 重复检测        │  三层检测算法（详见下文）
│  (Repeat Detection)     │  标记 keep=False
└─────────────────────────┘
   │
┌─────────────────────────┐
│  Step 2c: AI 重录标记    │  应用外部 AI 分析结果
│  (AI Retake Marking)    │  按句子ID标记 keep=False
└─────────────────────────┘
   │
┌─────────────────────────┐
│  Step 3: FFmpeg 精准剪辑 │  从原始文件裁切保留片段
│  合并 + 归一化           │  拼接 → normalize → 输出
└─────────────────────────┘
   │
   ▼
输出音频 + 报告
```

### 核心数据结构

#### Segment（词级片段）
```python
@dataclass
class Segment:
    start: float    # 起始时间（秒）
    end: float      # 结束时间（秒）
    text: str       # 转录文本
    keep: bool      # True=保留, False=删除
    removal_reason: str  # 删除原因
```

每个 Segment 对应 Whisper 输出的一个"词"（中文通常是 2-4 个字的片段）。整个处理流程通过修改 `keep` 字段来标记需要删除的片段。

#### Sentence（句子级分组）
```python
@dataclass
class Sentence:
    start: float
    end: float
    text: str
    word_segments: list[Segment]  # 包含的词级片段
    keep: bool
```

通过 `_group_into_sentences()` 将词级片段按停顿间隔分组为句子。默认停顿阈值 0.4 秒——两个词之间如果超过 0.4 秒没有语音，就被视为不同的句子。

### 重复检测算法（三层）

重复检测是本工具的核心算法，采用三层递进检测策略：

#### 第一层：句子级重复检测（Pass 1）

在滑动窗口内（默认 window=8），对每对句子进行三种比较：

**1. 近似重复（similarity ≥ 0.55）**
- 使用 `difflib.SequenceMatcher` 计算文本相似度
- 阈值 0.55 意味着超过一半的内容相同即判定为重复
- 始终保留后一个（更完整的版本），删除前一个
- 时间限制：两句之间不超过 15 秒

```
示例：
  句子A："今天我们来讲一下关于机器学习"     ← 删除
  句子B："今天我们来讲一下关于机器学习的基础概念"  ← 保留
  相似度：0.72 ≥ 0.55 → 判定为重复
```

**2. 前缀重试（Prefix Retry）**
- 检测"说了半句话就停下来重新说"的模式
- 判断条件：句子A 是句子B 的前缀部分（overlap ≥ 0.5）
- 句子A 不能比句子B 长超过 30%

```
示例：
  句子A："我们可以看到这个"           ← 删除（前缀重试）
  句子B："我们可以看到这个数据的变化趋势"  ← 保留
```

**3. 短语级重复（Phrase Repeat）**
- 使用最长公共子串（LCS）算法
- 如果两句共享 ≥ 6 个字符的公共子串，或子串长度 ≥ 较短句子的 50%
- 用于捕捉句子整体不同但关键短语重复的情况

```
示例：
  句子A："右边这张照片是上周拍的"        ← 删除
  句子B："然后右边这张照片展示的是最新数据"  ← 保留
  LCS = "右边这张照片" (6字) → 判定为短语重复
```

#### 第一层附加：粗粒度重复检测（Pass 1b）

使用更大的停顿阈值（0.8 秒）重新分组句子，然后再跑一遍相同的检测逻辑。这是因为细粒度分组（0.4 秒）可能把一个完整的重复拆成多段而漏检。

#### 第二层：单词级重复（Pass 2）

检测连续出现的相同单词（结巴/口吃）：
- 相邻两个保留的词如果文本完全相同，且间隔 < 2 秒，删除前一个

```
示例：
  "我们" "我们" "需要考虑"  →  "我们" "需要考虑"
```

### AI 重录识别

`apply_ai_retakes()` 函数接收外部 AI 分析返回的句子 ID 列表，将对应句子标记为删除。

工作流程：
1. 重新调用 `_group_into_sentences()` 将当前存活的词级片段分组为句子
2. 遍历 AI 返回的 ID 列表，将对应句子的所有词级片段标记为 `keep=False`
3. 统计到 `CleanupReport` 中

注意：AI 重录标记在文本检测之后执行，因此句子的编号可能因前面的检测而发生变化。建议使用 `--transcribe-only` 独立生成的 `sentences.json` 中的 ID。

### 音频重建（FFmpeg 精准剪辑）

`rebuild_audio_ffmpeg()` 的处理步骤：

1. **收集保留范围**：遍历所有 `keep=True` 的 Segment，收集时间范围，每段前后各扩展 30ms 作为缓冲
2. **合并相邻范围**：如果两段间隔 ≤ 80ms，合并为一段（避免产生太多碎片）
3. **FFmpeg 精准裁切**：对每个保留范围，从原始文件裁切出 WAV 片段
4. **FFmpeg 拼接**：使用 concat demuxer 将所有片段无缝拼接
5. **归一化**：用 pydub 的 `normalize()` 统一音量
6. **导出**：按目标格式输出最终文件

这种方式的优势：
- 直接从原始文件裁切，不经过中间编解码，保持音质
- FFmpeg 的时间戳精度远高于 pydub 的毫秒级操作
- 处理速度快（30 分钟音频约 30 秒完成重建）

### 转录缓存机制

Whisper 转录是最耗时的步骤（medium 模型处理 30 分钟音频约需 25 分钟）。为了支持快速迭代，实现了缓存机制：

- 转录结果保存为 `{filename}_transcript.json`（词级别）和 `{filename}_sentences.json`（句子级别）
- 后续运行时自动检测缓存文件，跳过转录步骤
- 缓存文件格式为标准 JSON，可以手动编辑或用于其他分析

## 处理模式

| 模式 | 说明 | 适用场景 |
|------|------|----------|
| `silence` | 仅去除长停顿 | 快速处理，保守策略 |
| `filler` | 去除停顿 + 口头禅 | 日常对话清理 |
| `full` | 停顿 + 口头禅 + 文本重复检测 | 通用全功能清理 |
| `ai` | 同 full + AI 重录识别 | 重录频繁的专业录音 |

## 参数说明

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--language` | zh | 语言：zh（中文）或 en（英文） |
| `--mode` | full | 处理模式：silence / filler / full / ai |
| `--model-size` | medium | Whisper 模型：tiny / base / small / medium / large-v3 |
| `--min-silence` | 700 | 最小静音时长（ms），低于此值的停顿不处理 |
| `--keep-silence` | 250 | 保留的停顿时长（ms），保持自然节奏 |
| `--silence-thresh` | -40 | 静音阈值（dB），低于此值视为静音 |
| `--transcribe-only` | — | 仅转录，不处理音频 |
| `--ai-retakes` | — | AI 重录标记文件（JSON 句子 ID 数组） |
| `--report` | — | 生成 JSON 格式的处理报告 |

## 输出示例

处理 30 分钟中文录音的典型结果：

```
=== Audio Cleanup Report ===
Input:  recording.wav (1786.6s)
Output: recording_cleaned.wav (829.0s)
Saved:  957.6s (53.6%)

Silences removed: 0 (0.0s)
Breaths removed:  0 (0.0s)
Fillers removed:  43
  Filler words: 嗯, 啊, 然后, 对吧, 就是, ...
Repeats removed: 118
  Repeated: [AI] #0: 大家好..., [REPEAT 72%] 今天我们→今天我们来, ...
```

## 已知限制

1. **Whisper 转录精度**：中文分词偶有误差，可能影响重复检测的准确度
2. **阈值敏感性**：相似度阈值（0.55）和停顿阈值（0.4s）对不同录音风格可能需要调整
3. **AI 句子ID偏移**：如果先运行文本检测再应用 AI 标记，句子编号可能有偏移。建议使用 `--transcribe-only` 生成的原始句子 ID
4. **语言支持**：目前仅支持中文和英文的口头禅检测，但 Whisper 转录支持更多语言

## 技术依赖

- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) — 高性能 Whisper 推理引擎（CTranslate2 后端）
- [pydub](https://github.com/jiaaro/pydub) — 音频处理库
- [FFmpeg](https://ffmpeg.org/) — 音视频编解码工具

## 许可证

MIT License
