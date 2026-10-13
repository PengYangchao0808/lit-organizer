---
name: lit-organizer
description: 文献一条龙工具：从 DOI 下载 PDF（Sci-Hub 镜像链 + OA/PMC-PoW 回退，脚本 scripts/download.py），提取元数据（年份、通讯作者、期刊、标题），按研究主题自动分类，并按"年份_作者_期刊_标题关键词（3-6实词）"格式批量重命名文件（scripts/rename.py），内置化学字符归一化（下标数字、希腊字母）。适用于化学、生物、材料等实验科学领域的文献整理。
---

# Literature Organizer (lit-organizer)

你是一个学术文献整理专家。你的职责是从PDF文献中提取关键元数据，按研究主题自动分类，并按照统一格式批量重命名文件。

## 适用场景

当用户提供以下请求时，使用本Skill：

- "给我这几个 DOI，下载文献并整理好"（一条龙：下载→重命名）
- "下载这篇文献"、"从 Sci-Hub 下载"
- "整理文献"、"分类文献"、"重命名PDF"
- "按主题分类我的论文"
- "把文献按作者-年份-期刊格式重命名"
- 目录中包含大量学术PDF需要规范化管理
- 基金撰写、课题组文献库整理、博硕论文参考文献管理

## 前置依赖

- Python 3.8+
- PyPDF2 (`pip install PyPDF2`) — 重命名阶段用
- 下载阶段**纯标准库**，无额外依赖

验证：
```bash
python3 -c "import PyPDF2; print('OK')"
```

## 工作流程概览

```
Phase 0: DOWNLOAD → DOI → PDF（Sci-Hub 镜像链 + OA/PMC-PoW 回退 + 元数据 sidecar）
Phase 1: EXTRACT  → 从 sidecar 或 PDF 提取元数据
Phase 2: CLASSIFY → 按研究主题关键词分类
Phase 3: RENAME   → 化学归一化 + 关键词标题，按"年份_作者_期刊_标题"重命名
Phase 4: VERIFY   → 验证结果，人工修正问题文件与关键词质量
```

**可直接运行的固化脚本（一条龙，以它们为准）**：
- `scripts/download.py` — Phase 0
- `scripts/rename.py` — Phase 1+3（sidecar 优先 → 文件名回退 → PDF 提取）

---

## Phase 0: DOWNLOAD — DOI → PDF（一条龙入口）

### 用法

```bash
SKILL=~/.config/opencode/skills/lit-organizer/scripts

# 单篇 / 多篇 DOI
python3 $SKILL/download.py 10.1021/ja00527a032 --out ./papers

# 批量（每行一个 DOI，# 开头为注释）
printf '# SN2 论文\n10.1021/ja00466a059\n10.1016/0040-4039(84)80012-X\n' > dois.txt
python3 $SKILL/download.py --file dois.txt --out ./papers
```

参数：`--out`（默认当前目录）、`--sleep`（DOI 间隔秒数，默认 1.5）

### 策略链（每个 DOI 依次执行）

```
1. Sci-Hub 镜像链（实测优先级：.ee → .vg → .ren → .ru → .shop → .tf → .box → .st）
   └ 解析文章页 <iframe id="pdf"> → 从无防护 PDF 宿主（sci.bban.top）直连下载
   └ 挑战页 / 403 / 未收录 / 非 PDF → 记录原因，跳下一镜像
2. OA 回退：Semantic Scholar（免 key）
   ├ 有 PubMedCentral ID → 解 SHA-256 PoW（difficulty=4，秒级算出）取 PMC PDF
   └ 否则 → 尝试 openAccessPdf 出版商直链
3. 全失败 → 打印每镜像失败原因，退出码 1
```

### 输出（供 rename 阶段消费）

| 文件 | 说明 |
|------|------|
| `10.1021_ja00527a032.pdf` | 临时名（DOI，`/`→`_`），rename 后转正 |
| `10.1021_ja00527a032.meta.json` | 元数据 sidecar：title/year/venue/first_author；**rename 优先读它**，重命名后随文件改名保留 |

### 已知环境行为（实测）

- Sci-Hub 对**未收录 DOI**（2021 年后新文献）返回 Verification 页而非 404 → 属正常，自动落入 OA 回退
- PMC 短时间重复请求会升级为 reCAPTCHA → 冷却几分钟后恢复 PoW，重跑即可
- 镜像批量连打可能触发 IP 限流 → 调大 `--sleep` 或换时间重试
- 全部镜像同时 challenge ≠ 脚本坏，是环境限流；脚本已给出每镜像原因日志

### 一条龙完整命令

```bash
python3 $SKILL/download.py --file dois.txt --out ./papers && \
python3 $SKILL/rename.py --dir ./papers
# → 再把新文件行追加到目录的 文献清单.md
```

---

## Phase 1: EXTRACT — 元数据提取

### 1.1 提取策略

对每个PDF，从前3-5页提取以下四元组：

| 字段 | 提取优先级 | 说明 |
|------|-----------|------|
| **year** | 文件名 > PDF文本 | 4位年份，范围1900-2025 |
| **author** | 星标(*) > 通讯作者行 > 邮箱 | 仅提取姓氏(Last Name) |
| **journal** | PDF文本头部 > DOI前缀推断 | 使用标准缩写 |
| **title** | PDF正文前几行 | 排除元数据行 |

### 1.2 通讯作者识别（核心逻辑）

**最重要的规则：通讯作者通常在作者列表中用星标(*)标记。**

#### 模式1: 星标在作者名后
```
Benjamin List*
David A. Evans,* Zachary K. Sweeney, Tomislav Rovis
MarcL. Snapper*
Annaliese K. Franz*
```

提取正则：
```python
r'([A-Z][a-z]+(?:\s+[A-Z]\.?\s*)*(?:\s+[A-Z][a-z]+)*)\s*\*'  # "Name*"
r'([A-Z][a-z]+(?:\s+[A-Z]\.?\s*)*(?:\s+[A-Z][a-z]+)*)\s*\d*,?\d*\s*\*'  # "Name1,2*"
```

#### 模式2: 星标在多作者行末尾
```
Junfeng Yang, *,† Qiao Sun, and Naohiko Yoshikai *
```

正则：匹配行中最后一个带`*`的完整人名。

#### 模式3: 通讯作者行明确标注
```
*To whom correspondence should be addressed. E-mail: xxx@xxx
*Corresponding author. Department of Chemistry...
```

正则：
```python
r'[∗*]\s*(?:Corresponding author|To whom correspondence should be addressed)'
r'E-mail\s*:\s*([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})'
```

#### 模式4: 仅邮箱可用
从邮箱用户名提取姓氏：
```python
def extract_author_from_email(email):
    username = email.split('@')[0]
    username = re.sub(r'[^a-zA-Z]', '', username)
    return username.capitalize() if username else None
```

### 1.3 期刊识别

#### 标准缩写映射表
```python
JOURNAL_MAP = {
    r'Angew\s*Chem\s*Int\s*Ed': 'Angew Chem Int Ed',
    r'Nature': 'Nature',
    r'JACS|Journal\s*of\s*the\s*American\s*Chemical\s*Society': 'JACS',
    r'Organic\s*Letters|Org\s*Lett': 'Org Lett',
    r'Accounts\s*of\s*Chemical\s*Research|Acc\s*Chem\s*Res': 'Acc Chem Res',
    r'Chemical\s*Communications|Chem\s*Commun': 'Chem Commun',
    r'Chemical\s*Science|Chem\s*Sci': 'Chem Sci',
    r'Organic\s*&\s*Biomolecular\s*Chemistry|Org\s*Biomol\s*Chem': 'Org Biomol Chem',
    r'Organic\s*Chemistry\s*Frontiers|Org\s*Chem\s*Front': 'Org Chem Front',
    r'Phytochemistry': 'Phytochemistry',
    r'Fitoterapia': 'Fitoterapia',
    r'Chemistry\s*Letters|Chem\s*Lett': 'Chem Lett',
    r'Dalton\s*Transactions|Dalton\s*Trans': 'Dalton Trans',
    # --- 2026-10 增补（顺序敏感：多词/带限定的模式必须放在泛化模式之前） ---
    r'Tetrahedron\s*Lett(?:ers)?': 'Tetrahedron Lett',   # 必须在 Tetrahedron 之前
    r'Tetrahedron(?!\.?\s*Lett)': 'Tetrahedron',
    r'J\s*Org\s*Chem|Journal\s*of\s*Organic\s*Chemistry': 'J Org Chem',
    r'Tetrahedron\s*Asymmetry': 'Tetrahedron Asymmetry',
    r'Synlett': 'Synlett',
    r'Synthesis': 'Synthesis',
}
```

#### DOI前缀推断
```python
# 匹配规则：按前缀长度降序检查（更精确的前缀优先），勿按字典序
DOI_TO_JOURNAL = {
    # 精确前缀（须先于泛化前缀匹配）
    '10.1016/0040-4039': 'Tetrahedron Lett',
    '10.1016/0040-4027': 'Tetrahedron',
    '10.1016/0040-4028': 'Tetrahedron Asymmetry',
    '10.1021/jo': 'J Org Chem',
    '10.1021/ol': 'Org Lett',
    '10.1021/ja': 'JACS',
    '10.1002/anie': 'Angew Chem Int Ed',
    # 泛化前缀（兜底）
    '10.1038/': 'Nature',
    '10.1039/': 'Chem Commun',   # 实际含 Chem Sci，须先查 JOURNAL_MAP 正文
    '10.1016/': 'Elsevier',
}
```

### 1.4 标题提取

从PDF文本中寻找第一个"看起来像标题"的行：

```python
def is_title_line(line):
    if len(line) < 15 or len(line) > 200:
        return False
    # 排除元数据行
    skip_words = ['received', 'published', 'doi:', 'copyright', 'abstract',
                  'introduction', 'references', 'supporting information',
                  'corresponding author', 'e-mail']
    if any(w in line.lower() for w in skip_words):
        return False
    # 排除全大写或全小写
    if line == line.upper() or line == line.lower():
        return False
    return True
```

### 1.5 年份提取

```python
YEAR_PATTERNS = [
    r'(\d{4})\s*(?:©|Copyright)',
    r'(?:Received|Published|Available online)\s*:?\s*\d{1,2}\s+\w+\s+(\d{4})',
    r'©\s*(\d{4})',
    r'\((\d{4})\)',
    r'doi:.*?(\d{4})',
]
# 验证范围: 1900-2025
```

---

## Phase 2: CLASSIFY — 主题分类

### 2.1 分类流程

1. 对每个PDF提取前3页文本
2. **优先级规则匹配**（高优先级规则先匹配，避免类别冲突）
3. 标题关键词优先于正文关键词
4. 无法匹配时归入"基础综述与概念"

### 2.2 分类体系（按催化类型 + 文献类型，避免交叉污染）

**核心原则：一个文献只属于一个类别，按"主要内容"归类。**

```python
CATEGORIES = {
    "01_基础综述与概念": {
        "desc": "领域综述、开创性概念、方法学总论、无法明确分类的文献",
        "keywords": ["review", "perspective", "strategies", "new strategies",
                     "journey", "total syntheses", "biomolecular chemistry"],
    },
    "02_Lewis酸催化": {
        "desc": "金属Lewis酸催化、手性配体、噁唑硼烷、铜催化",
        "keywords": ["Lewis acid", "oxazaborolidine", "chiral ligand",
                     "scandium", "copper", "Cu(I)", "boron"],
    },
    "03_有机催化_胺催化": {
        "desc": "有机催化中的胺催化、烯胺/亚胺离子/SOMO",
        "keywords": ["organocatalysis", "organocatalyst", "amine", "enamine",
                     "iminium", "proline", "SOMO", "MacMillan"],
    },
    "04_IDPi_Bronsted酸催化": {
        "desc": "IDPi催化、手性Brønsted酸催化",
        "keywords": ["IDPi", "Brønsted acid", "confined acid"],
    },
    "05_烯丙基化与Sakurai反应": {
        "desc": "烯丙基化、Sakurai反应、Hosomi-Sakurai、烯丙基硅烷",
        "keywords": ["allylation", "Sakurai", "allylsilane", "Hosomi"],
    },
    "06_环化与成环反应": {
        "desc": "环化反应、[3+2]、[4+2]、carboannulation、Diels-Alder、spirooxindole",
        "keywords": ["carboannulation", "cycloaddition", "Diels-Alder",
                     "annulation", "cyclization", "[3+2]", "[4+2]",
                     "spirooxindole", "cyclopentene"],
    },
    "07_天然产物全合成": {
        "desc": "天然产物全合成、萜类、三萜、天然产物化学综述",
        "keywords": ["total synthesis", "natural product", "triterpenoid",
                     "schinortriterpenoid", "Schisandraceae", "terpene"],
    },
    "08_植物化学与分离": {
        "desc": "植物化学成分分离鉴定（非合成），通过期刊判断",
        "keywords": ["phytochemistry", "fitoterapia", "isolation",
                     "characterization", "plant"],
    },
    "09_反应机理与立体化学": {
        "desc": "反应机理研究、立体化学、构效关系",
        "keywords": ["mechanism", "stereochemistry", "bromodemetalation",
                     "stereoselectivity"],
    }
}
```

### 2.3 分类算法（优先级规则，非简单关键词计数）

```python
def classify_paper(text, title):
    """按优先级从高到低匹配，避免类别冲突。"""
    text_lower = text.lower()
    title_lower = title.lower()

    # === 优先级1：期刊判断 ===
    if "phytochemistry" in text_lower[:500] or "fitoterapia" in text_lower[:500]:
        return "08_植物化学与分离"

    # === 优先级2：天然产物 ===
    if any(kw in text_lower for kw in ["triterpenoid", "schinortriterpenoid",
                                        "schisandraceae"]):
        return "07_天然产物全合成"

    # === 优先级3：IDPi / Brønsted酸 ===
    if "idpi" in text_lower:
        return "04_IDPi_Bronsted酸催化"

    # === 优先级4：SOMO ===
    if "somo activation" in text_lower:
        return "03_有机催化_胺催化"

    # === 优先级5：有机催化 ===
    if any(kw in text_lower for kw in ["organocatalysis", "organocatalyst",
                                        "enamine catalysis", "iminium catalysis"]):
        return "03_有机催化_胺催化"

    # === 优先级6：烯丙基化（标题优先） ===
    if any(kw in title_lower for kw in ["sakurai", "allylation", "hosomi"]):
        return "05_烯丙基化与Sakurai反应"
    allylation_count = sum(text_lower.count(kw) for kw in
                          ["allylation", "sakurai", "hosomi", "allylsilane"])
    if allylation_count >= 3:
        return "05_烯丙基化与Sakurai反应"

    # === 优先级7：环化反应（标题优先） ===
    if any(kw in title_lower for kw in ["carboannulation", "cycloaddition",
                                          "spirooxindole", "diels-alder"]):
        return "06_环化与成环反应"

    # === 优先级8：Lewis酸催化 ===
    if any(kw in text_lower for kw in ["oxazaborolidine", "chiral lewis acid",
                                        "scandium catalyst", "copper catalyst"]):
        return "02_Lewis酸催化"

    # === 优先级9：综述 ===
    if any(kw in title_lower for kw in ["total synthesis", "journey",
                                         "review", "perspective"]):
        return "01_基础综述与概念"

    # === 优先级10：机理 ===
    if "bromodemetalation" in text_lower:
        return "09_反应机理与立体化学"

    # === 默认 ===
    return "01_基础综述与概念"
```

### 2.4 关键改进点

| 旧方案问题 | 新方案改进 |
|-----------|-----------|
| "不对称催化"是垃圾桶 | 取消该类别，按催化类型细分 |
| "合成方法"杂乱 | 拆分为"天然产物"、"环化"、"机理"等 |
| 简单关键词计数导致误分类 | 优先级规则，标题关键词优先 |
| 全合成综述和方法学混放 | 综述归"基础综述"，全合成归"天然产物" |
| 植物化学混入合成方法 | 通过期刊判断，单独分类 |

### 2.5 自动创建目录并移动文件

```python
for category, files in classification.items():
    category_dir = pdf_dir / category
    category_dir.mkdir(exist_ok=True)
    for f in files:
        shutil.move(str(pdf_dir / f), str(category_dir / f))
```

---

## Phase 3: RENAME — 文件重命名

> 可直接运行：`python3 scripts/rename.py --dir <目录>`（`--dry-run` 预览 / `--force` 重处理已规范化的文件 / 退出码 1 = 有失败文件）

### 3.1 目标格式

```
{年份}_{通讯作者姓氏}_{期刊缩写}_{标题关键词}.pdf
```

**标题字段 = 关键词式（3-6 个实词），不是完整标题**（2026-10 起生效）：

1. 只取标题第一子句（第一个 `". "` 之前的部分；双句式长标题常见）
2. 删除停用词（虚词 + 程度副词）
3. 保留 3-6 个内容词——**连字符化合物词整体保留算一个词**（如 `Copper-Catalyzed`）
4. 超过 6 个时保留前 6 个，**VERIFY 阶段人工复核是否丢核心概念**（必要时手挑）

```python
STOPWORDS = {'a', 'an', 'the', 'of', 'in', 'on', 'to', 'towards', 'toward',
             'with', 'via', 'for', 'and', 'or', 'by', 'from', 'under',
             'into', 'involving', 'using', 'between', 'is', 'are', 'at', 'as',
             'very', 'remarkably', 'completely', 'efficiently', 'highly'}
```

示例（完整标题 → 关键词式）：

| 完整标题 | 结果 |
|---------|------|
| Copper-Catalyzed Asymmetric Allylic Alkylation of Racemic Inert Cyclic Allylic Ethers under Batch and Flow Conditions | `2023_Li_Chem_Sci_Copper-Catalyzed_Asymmetric_Allylic_Alkylation_Cyclic_Ethers.pdf` |
| Reactivity of RCu,BF₃ and R₂CuLi,BF₃ Towards Allylic Acetals and Ethers | `1984_Ghribi_Tetrahedron_Lett_RCu_R2CuLi_BF3_Allylic_Acetals_Ethers.pdf` |
| Enantioselective Organocatalysis (short) | `2016_Benjamin_List_Angew_Chem_Int_Ed_Enantioselective_Organocatalysis.pdf` |

### 3.2 化学字符归一化（清洗前必做）

化学符号直接走 `clean()` 会**丢信息**：`BF₃`→`BF`（下标₃非word字符被剥掉）、`γ-Attack`→`Attack`（若先去标点）。必须先归一化：

```python
def normalize_chemistry(text):
    # 1) 下标/上标数字 → ASCII 数字
    trans = str.maketrans('₀₁₂₃₄₅₆₇₈₉⁰¹²³⁴⁵⁶⁷⁸⁹',
                          '01234567890123456789')
    text = text.translate(trans)
    # 2) 希腊字母 → 拼写（ASCII 安全、跨平台可检索）
    for k, v in {'α': 'alpha', 'β': 'beta', 'γ': 'gamma', 'δ': 'delta',
                 'ε': 'epsilon', 'μ': 'mu', 'Δ': 'Delta', 'Ω': 'Omega',
                 'σ': 'sigma'}.items():
        text = text.replace(k, v)
    # 3) 中点/项目符号 → 空格（RCu·BF₃ → RCu BF3）
    text = text.replace('·', ' ').replace('•', ' ')
    return text
```

**处理顺序（固定）**：
```
提取元数据 → normalize_chemistry → 关键词抽取(3.1) → clean_filename_part(3.3) → 拼接
```

### 3.3 文件名清洗规则

```python
def clean_filename_part(text, max_len=80):
    if not text:
        return "Unknown"
    text = re.sub(r'[^\w\s-]', '', text)   # 只保留字母数字空格连字符
    text = re.sub(r'\s+', '_', text)        # 空格转下划线
    text = re.sub(r'_+', '_', text)         # 合并连续下划线
    text = text.strip('_')
    if len(text) > max_len:                 # 兜底截断（关键词模式下很少触发）
        text = text[:max_len].rsplit('_', 1)[0]
    return text
```

> 注：标题长度由 3.1 的**词数上限（6词）**控制，`max_len=80` 仅为兜底，不再用旧版 50 字符截断（会拦腰截断 `Copper-Catalyzed_..._Cyclic_Ethers` 这类长关键词组）。

### 3.4 命名冲突处理

```python
if new_path.exists() and new_path != old_path:
    base = new_path.stem
    counter = 1
    while new_path.exists():
        new_path = parent / f"{base}_{counter}.pdf"
        counter += 1
```

---

## Phase 4: VERIFY — 验证与修正

### 4.1 自动验证

重命名后检查：
1. 总文件数是否一致（无遗漏、无丢失）
2. 是否存在`Unknown`字段（需人工确认）
3. 是否有命名冲突
4. **关键词标题质量**：3-6 词是否丢掉核心概念（研究对象/反应类型）？丢了就手改文件名

### 4.2 问题文件处理

对包含`Unknown`或提取明显错误的文件：
1. 用`PyPDF2`重新提取全文
2. 搜索`*`标记的作者名
3. 搜索`Corresponding author`行
4. 搜索`E-mail`行
5. 如仍无法识别，列入"需人工检查"清单

### 4.3 输出报告

生成 `分类总结_Classification_Summary.md`：

```markdown
# 文献分类总结

## 总计: N 篇文献

### 类别1
**描述**: ...
**数量**: M 篇
- file1.pdf
- file2.pdf

## 分类方法
1. 从PDF前3页提取文本
2. 基于关键词频率分类
3. 按"年份_作者_期刊_标题"格式重命名

## 注意事项
- 部分文献涉及多主题，仅按主要关键词分类
- "其他"类别需人工进一步确认
```

---

## 完整执行脚本模板

> **权威版本在 `scripts/rename.py`（含 sidecar 消费、文件名回退、幂等保护、--dry-run/--force，以它为准）**。以下内联模板仅供理解逻辑与自定义改造。

将以下脚本保存为 `/tmp/lit_organizer.py` 并执行：

```python
#!/usr/bin/env python3
"""Literature Organizer: Extract, Classify, Rename."""

import os, re, json, shutil
from pathlib import Path
from collections import Counter
import PyPDF2

# ============ CONFIG ============
PDF_DIR = Path("TARGET_DIRECTORY_HERE")  # 修改为目标目录
MAX_PAGES = 3
# ================================

def extract_text(pdf_path, max_pages=3):
    try:
        with open(pdf_path, 'rb') as f:
            reader = PyPDF2.PdfReader(f)
            return "\n".join(
                page.extract_text() or ""
                for i, page in enumerate(reader.pages)
                if i < max_pages
            )
    except Exception as e:
        return ""

def extract_author(text):
    """Extract corresponding author marked with asterisk (*)."""
    for line in text.split('\n'):
        # Pattern: "Name*" or "Name1,2*"
        m = re.search(r'([A-Z][a-z]+(?:\s+[A-Z]\.?\s*)*(?:\s+[A-Z][a-z]+)*)\s*\d*,?\d*\s*\*$', line)
        if m:
            return m.group(1).strip()
    # Fallback: email
    m = re.search(r'E-mail\s*:\s*([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+)', text)
    if m:
        username = m.group(1).split('@')[0]
        return re.sub(r'[^a-zA-Z]', '', username).capitalize()
    return None

def extract_year(text):
    for p in [r'(\d{4})\s*©', r'\((\d{4})\)', r'©\s*(\d{4})']:
        m = re.search(p, text)
        if m and 1900 <= int(m.group(1)) <= 2025:
            return m.group(1)
    return None

def extract_journal(text):
    JMAP = {
        r'Angew\s*Chem\s*Int\s*Ed': 'Angew Chem Int Ed',
        r'Nature': 'Nature',
        r'JACS': 'JACS',
        r'Org\s*Lett': 'Org Lett',
        r'Chem\s*Commun': 'Chem Commun',
        r'Chem\s*Sci': 'Chem Sci',
        r'Org\s*Biomol\s*Chem': 'Org Biomol Chem',
        r'Org\s*Chem\s*Front': 'Org Chem Front',
    }
    for pat, abbr in JMAP.items():
        if re.search(pat, text[:2000], re.IGNORECASE):
            return abbr
    return None

def extract_title(text):
    for line in text.split('\n'):
        line = line.strip()
        skip = ['received', 'published', 'doi:', 'copyright', 'abstract',
                'corresponding', 'e-mail', 'supporting']
        if len(line) > 15 and len(line) < 200:
            if not any(s in line.lower() for s in skip):
                if line != line.upper() and line != line.lower():
                    return re.sub(r'[^\w\s-]', '', line).strip()
    return None

def normalize_chemistry(text):
    """化学字符归一化：下标数字/希腊字母/中点 → ASCII（须在清洗前调用）。"""
    trans = str.maketrans('₀₁₂₃₄₅₆₇₈₉⁰¹²³⁴⁵⁶⁷⁸⁹', '01234567890123456789')
    text = text.translate(trans)
    for k, v in {'α': 'alpha', 'β': 'beta', 'γ': 'gamma', 'δ': 'delta',
                 'ε': 'epsilon', 'μ': 'mu', 'Δ': 'Delta', 'Ω': 'Omega',
                 'σ': 'sigma'}.items():
        text = text.replace(k, v)
    return text.replace('·', ' ').replace('•', ' ')

STOPWORDS = {'a', 'an', 'the', 'of', 'in', 'on', 'to', 'towards', 'toward',
             'with', 'via', 'for', 'and', 'or', 'by', 'from', 'under',
             'into', 'involving', 'using', 'between', 'is', 'are', 'at', 'as',
             'very', 'remarkably', 'completely', 'efficiently', 'highly'}

def extract_keywords(title, max_words=6):
    """标题 → 3-6 个关键词：第一子句 + 去停用词 + 连字符化合物整体保留。"""
    clause = title.split('. ')[0].rstrip('.,;:')
    clause = normalize_chemistry(clause)
    tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9\-']*", clause)
    words = [t for t in tokens if t.lower() not in STOPWORDS] or tokens
    return words[:max_words]

def clean(s, max_len=80):
    """清洗单字段。标题长度由 extract_keywords 词数控制，此处仅兜底。"""
    if not s: return "Unknown"
    s = re.sub(r'[^\w\s-]', '', s)
    s = re.sub(r'\s+', '_', s)
    s = re.sub(r'_+', '_', s).strip('_')
    return s[:max_len].rsplit('_', 1)[0] if len(s) > max_len else s

# ============ MAIN ============
pdfs = list(PDF_DIR.glob("**/*.pdf"))
results = []
for pdf in pdfs:
    # 幂等保护：已符合 YYYY_ 开头四段式命名的文件跳过，
    # 防止重跑时启发式关键词覆盖人工复核优化过的名字（需强制处理时手动注释此行）
    if re.match(r'^\d{4}_[A-Za-z][A-Za-z0-9\-]*_.+_.+\.pdf$', pdf.name):
        print(f"SKIP (normalized): {pdf.name}")
        continue
    text = extract_text(pdf)
    year = extract_year(text) or "Unknown"
    author = extract_author(text) or "Unknown"
    journal = extract_journal(text) or "Unknown"
    title = extract_title(text) or "Unknown"
    # 标题 → 关键词式（归一化内置于 extract_keywords）
    kws = extract_keywords(title)
    title_part = '_'.join(clean(w) for w in kws) if kws else "Unknown"
    new_name = f"{clean(year)}_{clean(author)}_{clean(journal)}_{title_part}.pdf"
    new_path = pdf.parent / new_name
    if new_path.exists() and new_path != pdf:
        base, c = new_path.stem, 1
        while new_path.exists():
            new_path = pdf.parent / f"{base}_{c}.pdf"; c += 1
    pdf.rename(new_path)
    results.append({"old": pdf.name, "new": new_path.name, "year": year,
                     "author": author, "journal": journal, "title": title})
    print(f"OK: {pdf.name} -> {new_path.name}")

# Save log
log_path = PDF_DIR / "rename_log.json"
with open(log_path, "w", encoding="utf-8") as f:
    json.dump(results, f, indent=2, ensure_ascii=False)
print(f"\nDone. {len(results)} files processed. Log: {log_path}")
```

### 带分类的完整脚本

如果需要同时执行分类，将以下脚本保存为 `/tmp/lit_organizer_classify.py`：

```python
#!/usr/bin/env python3
"""Literature Organizer with Classification."""

import re, shutil
from pathlib import Path
import PyPDF2

PDF_DIR = Path("TARGET_DIRECTORY_HERE")

def extract_text(pdf, n=3):
    try:
        with open(pdf, 'rb') as f:
            r = PyPDF2.PdfReader(f)
            return "\n".join((p.extract_text() or "") for i, p in enumerate(r.pages) if i < n)
    except: return ""

def classify(text, title):
    """按优先级分类，避免类别冲突。"""
    t = text.lower(); tl = title.lower()
    # 期刊判断
    if "phytochemistry" in t[:500] or "fitoterapia" in t[:500]:
        return "08_植物化学与分离"
    # 天然产物
    if any(k in t for k in ["triterpenoid","schinortriterpenoid","schisandraceae"]):
        return "07_天然产物全合成"
    # IDPi
    if "idpi" in t: return "04_IDPi_Bronsted酸催化"
    # SOMO
    if "somo activation" in t: return "03_有机催化_胺催化"
    # 有机催化
    if any(k in t for k in ["organocatalysis","organocatalyst","enamine catalysis","iminium catalysis"]):
        return "03_有机催化_胺催化"
    # 烯丙基化（标题优先）
    if any(k in tl for k in ["sakurai","allylation","hosomi"]):
        return "05_烯丙基化与Sakurai反应"
    if sum(t.count(k) for k in ["allylation","sakurai","hosomi","allylsilane"]) >= 3:
        return "05_烯丙基化与Sakurai反应"
    # 环化
    if any(k in tl for k in ["carboannulation","cycloaddition","spirooxindole","diels-alder"]):
        return "06_环化与成环反应"
    # Lewis酸
    if any(k in t for k in ["oxazaborolidine","chiral lewis acid","scandium catalyst","copper catalyst"]):
        return "02_Lewis酸催化"
    # 综述
    if any(k in tl for k in ["total synthesis","journey","review","perspective"]):
        return "01_基础综述与概念"
    # 机理
    if "bromodemetalation" in t: return "09_反应机理与立体化学"
    return "01_基础综述与概念"

# 执行分类
for pdf in sorted(PDF_DIR.glob("**/*.pdf")):
    if "元数据" in str(pdf): continue
    text = extract_text(pdf)
    title = ""
    for line in text.split('\n'):
        line = line.strip()
        skip = ['received','published','doi:','copyright','abstract','corresponding','e-mail']
        if 15 < len(line) < 200 and not any(s in line.lower() for s in skip):
            if line != line.upper() and line != line.lower():
                title = line; break
    cat = classify(text, title)
    dst = PDF_DIR / cat / pdf.name
    (PDF_DIR / cat).mkdir(exist_ok=True)
    if pdf.parent.resolve() != (PDF_DIR / cat).resolve() and not dst.exists():
        shutil.move(str(pdf), str(dst))
        print(f"[{cat:30s}] {pdf.name[:50]}")
```

---

## 自定义指南

### 修改分类关键词

编辑 `CATEGORIES` 字典，添加/删除类别和关键词。关键词应为小写。

### 修改目标目录

修改脚本顶部 `PDF_DIR = Path("...")` 为目标路径。

### 修改命名格式

修改 `new_name = f"..."` 模板。例如：
- `{year}_{author}_{title}.pdf` （省略期刊）
- `{author}-{year}-{journal}-{title}.pdf` （用连字符分隔）

---

## 注意事项

1. **PDF文本提取质量取决于PDF编码**：扫描版PDF无法提取文本，需OCR预处理
2. **星标(*)是最可靠的通讯作者标记**：优先使用此信号
3. **邮箱提取是最后手段**：用户名可能不是标准姓氏格式
4. **期刊识别依赖PDF头部文本**：部分PDF头部信息不完整
5. **标题可能包含OCR噪声**：建议人工抽查标题质量
6. **分类结果仅供参考**：多主题文献仅按主要关键词归类
