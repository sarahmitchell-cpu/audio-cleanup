# Audio Cleanup — 音频录音自动清理工具

一款基于 AI 的音频清理工具，自动去除录音中的**停顿、口头禅、重复内容和废话段落**，类似剪映的"删口癖"功能，但更强大——支持 AI 驱动的语义分析，智能识别重录片段。

## 功能特性

| 功能 | 说明 |
|------|------|
| **AI 驱动裁定** | 由 LLM 分析转录文本，仅保留通顺、不重复的完整句子 |
| 静音/停顿去除 | 自动检测并裁剪超长停顿，保留自然节奏 |
| 呼吸声检测 | 识别并去除句间呼吸声 |
| 口头禅/语气词去除 | 中文：嗯、啊、呃、那个、就是说等；英文：um、uh、you know 等 |
| 文本级重复检测 | 三层检测：句子级、短语级、单词级（可选，规则模式） |
| 精准剪辑 | 基于 FFmpeg 从原始文件精确裁切，避免二次编码损失 |
| 转录缓存 | Whisper 转录结果缓存为 JSON，后续处理无需重新转录 |
| **中间产物审计** | 每一步输出详细 JSON 文件，方便 debug 和审计 |

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

## 推荐流程：AI-Only 模式（两阶段）

对于重录内容较多的录音（如播客、旁白、课程录制），推荐使用纯 AI 裁定的两阶段流程。这种方式完全跳过基于固定规则的检测（口头禅匹配、文本相似度等），**由 AI 统一决定哪些句子保留**，避免误删。

### 阶段一：转录

```bash
# 仅转录，不处理音频（耗时约 25 分钟/30 分钟音频）
python3 audio_cleanup.py recording.wav --transcribe-only
```

生成两个文件：
- `recording_transcript.json` — 逐字级别的转录结果（词级时间戳）
- `recording_sentences.json` — 按停顿分组的句子列表（供 AI 分析）

#### `recording_transcript.json` 格式示例

每个元素是 Whisper 输出的一个"词"片段（中文通常 2-4 字）：

```json
[
  {
    "idx": 0,
    "start": 3.290,
    "end": 3.970,
    "text": "左边",
    "keep": true,
    "removal_reason": ""
  },
  {
    "idx": 1,
    "start": 3.970,
    "end": 4.370,
    "text": "这张",
    "keep": true,
    "removal_reason": ""
  }
]
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `idx` | int | 词片段序号（全局唯一） |
| `start` | float | 起始时间（秒） |
| `end` | float | 结束时间（秒） |
| `text` | str | 转录文本 |
| `keep` | bool | 是否保留（初始全部为 true） |
| `removal_reason` | str | 删除原因（初始为空） |

#### `recording_sentences.json` 格式示例

词片段按停顿（≥0.4秒）分组为句子：

```json
[
  {
    "id": 0,
    "start": 3.290,
    "end": 11.870,
    "text": "左边这张照片是1972年阿波罗17号拍摄的地球照片右边这张照片是2026年阿尔推米斯2号拍摄的地",
    "word_indices": [0, 1, 2, 3, 4, 5, ...]
  },
  {
    "id": 1,
    "start": 12.600,
    "end": 17.300,
    "text": "球照片右边这张照片是2026年阿尔推米斯2号拍摄的地球照片",
    "word_indices": [43, 44, 45, ...]
  }
]
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | int | 句子编号（供 AI 引用） |
| `start` | float | 句子起始时间（秒） |
| `end` | float | 句子结束时间（秒） |
| `text` | str | 句子完整文本 |
| `word_indices` | list[int] | 对应 transcript.json 中的词片段索引 |

### 阶段二：AI 分析 + 应用

将 `recording_sentences.json` 发送给 LLM（如 Claude、GPT-4），让 AI 分析并返回需要**删除**的句子 ID 列表。

**提示词示例：**
```
以下是一段录音的逐句转录（按停顿自动分组）。录音者在录制过程中经常说错后重新说。

请分析所有句子，返回需要删除的句子 ID 列表（JSON 数组格式）。

删除规则：
- 如果连续多句说的是同一段内容，保留最后一次（最完整的版本），前面的标记为删除
- 不完整的句子片段（如被截断、只有一两个字）应删除
- 内部有明显重复的句子（如"那时人类会那时人类会开启..."）应删除
- 结巴/断句的碎片应删除
- 只保留通顺、不重复的完整句子
- 不确定的句子不要删除

[粘贴 sentences.json 内容]
```

将 AI 返回的 ID 列表保存为 JSON 文件（如 `retakes.json`）：
```json
[0, 2, 3, 28, 29, 32, 33, 36, 44, 54, 55, ...]
```

然后使用 `ai-only` 模式应用，并保存所有中间产物：
```bash
python3 audio_cleanup.py recording.wav \
  --mode ai-only \
  --ai-retakes retakes.json \
  --save-intermediates \
  -o recording_cleaned.wav
```

## 处理流程详解

### 整体架构

根据 `--mode` 参数，工具有两条主要处理路径：

```
输入音频
   │
   ├──[缓存存在?]──→ 加载缓存转录
   │       否
   ▼
┌─────────────────────────────────┐
│  Step 1: Whisper 语音转录        │  faster-whisper, word_timestamps=True
│  生成逐字时间戳                  │  VAD 过滤，最小静音 300ms
│  输出: transcript.json           │
│  输出: sentences.json            │
└─────────────────────────────────┘
   │
   ├─── mode=ai-only ───────────────────────────────────────┐
   │                                                         │
   │    ┌─────────────────────────────────────────────┐      │
   │    │  Step 2: AI 裁定                             │      │
   │    │  读取 --ai-retakes 文件                      │      │
   │    │  按句子 ID 标记 keep=False                    │      │
   │    │  输出: intermediates/ai_only_decisions.json   │      │
   │    │  输出: intermediates/ai_only_kept_segments    │      │
   │    │  输出: intermediates/ai_only_removed_segments │      │
   │    └─────────────────────────────────────────────┘      │
   │                                                         │
   ├─── mode=full/ai ───────────────────────┐               │
   │                                         │               │
   │    ┌───────────────────────────┐       │               │
   │    │  Step 2a: 口头禅检测      │       │               │
   │    │  匹配预定义语气词列表     │       │               │
   │    └───────────────────────────┘       │               │
   │              │                          │               │
   │    ┌───────────────────────────┐       │               │
   │    │  Step 2b: 文本重复检测    │       │               │
   │    │  三层算法（句子/短语/词） │       │               │
   │    └───────────────────────────┘       │               │
   │              │                          │               │
   │    ┌───────────────────────────┐       │               │
   │    │  Step 2c: AI 重录标记     │       │               │
   │    │  (如提供 --ai-retakes)    │       │               │
   │    └───────────────────────────┘       │               │
   │                                         │               │
   ├─────────────────────────────────────────┘               │
   │                                                         │
   ◄─────────────────────────────────────────────────────────┘
   │
┌─────────────────────────────────┐
│  Step 3: FFmpeg 精准剪辑        │  从原始文件裁切保留片段
│  合并 + 归一化                   │  拼接 → normalize → 输出
└─────────────────────────────────┘
   │
   ▼
输出音频 + 报告 + 中间产物
```

### 中间产物（`--save-intermediates`）

启用 `--save-intermediates` 后，工具会在输入文件同目录下创建 `{filename}_intermediates/` 文件夹，保存每一步的详细数据。

#### 目录结构

```
Recording 58_intermediates/
├── ai_only_decisions.json        # 每个句子的保留/删除决策
├── ai_only_kept_segments.json    # 所有保留的词级片段（含时间戳和文本）
├── ai_only_removed_segments.json # 所有删除的词级片段（含时间戳、文本和原因）
├── ai_only_summary.txt           # 人类可读的决策摘要
└── final_report.json             # 最终处理报告（输入/输出时长、压缩率等）
```

#### `ai_only_decisions.json` 格式示例

```json
[
  {
    "sentence_id": 0,
    "text": "左边这张照片是1972年阿波罗17号拍摄的地球照片右边这张照片是2026年阿尔推米斯2号拍摄的地",
    "start": 3.29,
    "end": 11.87,
    "duration": 8.58,
    "decision": "REMOVE",
    "reason": "AI: fragment/repeat/incomplete",
    "word_count": 43,
    "kept_words": 0
  },
  {
    "sentence_id": 1,
    "text": "球照片右边这张照片是2026年阿尔推米斯2号拍摄的地球照片",
    "start": 12.6,
    "end": 17.3,
    "duration": 4.7,
    "decision": "KEEP",
    "reason": "",
    "word_count": 18,
    "kept_words": 18
  }
]
```

| 字段 | 说明 |
|------|------|
| `sentence_id` | 句子编号（对应 sentences.json 中的 id） |
| `text` | 句子文本 |
| `start/end` | 时间范围（秒） |
| `duration` | 句子时长（秒） |
| `decision` | `KEEP` 或 `REMOVE` |
| `reason` | 删除原因 |
| `word_count` | 包含的词片段数 |
| `kept_words` | 保留的词片段数 |

#### `ai_only_kept_segments.json` 格式示例

```json
[
  {"start": 12.6, "end": 12.84, "text": "球"},
  {"start": 12.84, "end": 13.22, "text": "照片"},
  {"start": 13.22, "end": 13.68, "text": "右边"},
  {"start": 13.68, "end": 14.2, "text": "这张照片"}
]
```

#### `ai_only_removed_segments.json` 格式示例

```json
[
  {"start": 3.29, "end": 3.97, "text": "左边", "reason": "AI retake: sentence 0"},
  {"start": 3.97, "end": 4.37, "text": "这张", "reason": "AI retake: sentence 0"},
  {"start": 4.37, "end": 4.81, "text": "照片", "reason": "AI retake: sentence 0"}
]
```

#### `ai_only_summary.txt` 格式示例

```
=== ai_only Stage Summary ===

Total sentences: 299
Kept: 222
Removed: 77

--- Kept Sentences ---
  [  1] (12.6-17.3s) 球照片右边这张照片是2026年阿尔推米斯2号拍摄的地球照片
  [  4] (25.4-28.3s) 好像1972年拍摄的要更通透
  [  5] (29.6-32.3s) 难道NASA的技术还不如以前了
  ...

--- Removed Sentences ---
  [  0] (3.3-11.9s) 左边这张照片是1972年阿波罗17号拍摄的地球照片...
         Reason: AI: fragment/repeat/incomplete
  [  2] (18.3-21.9s) 乍一看好像19
         Reason: AI: fragment/repeat/incomplete
  ...
```

#### `final_report.json` 格式示例

```json
{
  "input_file": "recording.wav",
  "output_file": "recording_cleaned.wav",
  "input_duration_sec": 1785.5,
  "output_duration_sec": 1043.4,
  "saved_sec": 742.1,
  "saved_pct": 41.6,
  "silences_removed": 0,
  "fillers_removed": 0,
  "repeats_removed": 77,
  "breaths_removed": 0,
  "filler_texts": [],
  "repeat_texts": ["[AI] #0: 左边这张照片...", "..."]
}
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

Whisper 转录是最耗时的步骤（medium 模型处理 30 分钟音频约需 25 分钟）。缓存机制：

- 转录结果保存为 `{filename}_transcript.json`（词级别）和 `{filename}_sentences.json`（句子级别）
- 后续运行时自动检测缓存文件，跳过转录步骤
- 缓存文件格式为标准 JSON，可以手动编辑或用于其他分析

## 处理模式

| 模式 | 说明 | 适用场景 |
|------|------|----------|
| `silence` | 仅去除长停顿 | 快速处理，保守策略 |
| `filler` | 去除停顿 + 口头禅 | 日常对话清理 |
| `full` | 停顿 + 口头禅 + 文本重复检测 | 通用全功能清理 |
| `ai` | 同 full + AI 重录识别 | 混合模式 |
| **`ai-only`** | **纯 AI 裁定，跳过所有规则** | **推荐：避免误删，效果最佳** |

## 参数说明

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--language` | zh | 语言：zh（中文）或 en（英文） |
| `--mode` | full | 处理模式：silence / filler / full / ai / ai-only |
| `--model-size` | medium | Whisper 模型：tiny / base / small / medium / large-v3 |
| `--min-silence` | 700 | 最小静音时长（ms），低于此值的停顿不处理 |
| `--keep-silence` | 250 | 保留的停顿时长（ms），保持自然节奏 |
| `--silence-thresh` | -40 | 静音阈值（dB），低于此值视为静音 |
| `--transcribe-only` | — | 仅转录，不处理音频 |
| `--ai-retakes` | — | AI 重录标记文件（JSON 句子 ID 数组） |
| `--save-intermediates` | — | 保存所有中间产物到 `{filename}_intermediates/` |
| `--report` | — | 生成 JSON 格式的处理报告 |

## 输出示例

### AI-Only 模式处理结果

```
=== Audio Cleanup Report ===
Input:  recording.wav (1785.5s)
Output: recording_cleaned.wav (1043.4s)
Saved:  742.1s (41.6%)

Silences removed: 0 (0.0s)
Breaths removed:  0 (0.0s)
Fillers removed:  0
Repeats removed: 77
```

## 规则模式：文本级重复检测算法（参考）

> 以下算法在 `full` 和 `ai` 模式中使用，`ai-only` 模式不使用。

采用三层递进检测策略：

### 第一层：句子级重复检测

在滑动窗口内（默认 window=8），对每对句子进行三种比较：

**1. 近似重复（similarity >= 0.55）**
- 使用 `difflib.SequenceMatcher` 计算文本相似度
- 始终保留后一个（更完整的版本），删除前一个

**2. 前缀重试（Prefix Retry）**
- 检测"说了半句话就停下来重新说"的模式

**3. 短语级重复（Phrase Repeat）**
- 使用最长公共子串（LCS）算法，阈值 >= 6 字符

### 第二层：单词级重复

检测连续出现的相同单词（结巴/口吃），间隔 < 2 秒。

## 已知限制

1. **Whisper 转录精度**：中文分词偶有误差，可能导致句子边界划分不准
2. **AI 模式依赖外部 LLM**：需要手动将 sentences.json 发送给 AI 并取回结果
3. **语言支持**：目前仅支持中文和英文的口头禅检测，但 Whisper 转录支持更多语言

## 技术依赖

- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) — 高性能 Whisper 推理引擎（CTranslate2 后端）
- [pydub](https://github.com/jiaaro/pydub) — 音频处理库
- [FFmpeg](https://ffmpeg.org/) — 音视频编解码工具

## 许可证

MIT License
