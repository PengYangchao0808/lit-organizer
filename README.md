# lit-organizer

OpenCode 文献一条龙 skill：**DOI → 下载 → 元数据 → 关键词重命名**，为化学/材料/生物文献库设计。

## 功能

- **下载**（`scripts/download.py`，纯标准库）
  - Sci-Hub 镜像链（实测优先级 `.ee → .vg → .ren → .ru …`），解析文章页 `<iframe id="pdf">` 从无防护宿主直连下载
  - 未收录/挑战页自动落 **OA 回退**：Semantic Scholar（免 key）→ PMC 解 SHA-256 PoW → 出版商直链
  - 同时产出 `.meta.json` 元数据 sidecar 供重命名消费
- **重命名**（`scripts/rename.py`，依赖 PyPDF2）
  - 命名格式：`{年份}_{作者}_{期刊缩写}_{标题关键词3-6实词}.pdf`
  - 化学归一化：`BF₃→BF3`、`R₂→R2`、`γ→gamma`、`RCu·BF₃→RCu_BF3`
  - 元数据优先级：sidecar > 人审文件名 > PDF 提取；幂等（已规范化的文件重跑跳过）

## 安装

克隆到 OpenCode skills 目录即可被识别：

```bash
git clone https://github.com/PengYangchao0808/lit-organizer.git \
  ~/.config/opencode/skills/lit-organizer
pip install PyPDF2   # 重命名阶段依赖
```

## 使用

```bash
SKILL=~/.config/opencode/skills/lit-organizer/scripts

# 一条龙：批量下载 + 规范化重命名
printf '10.1021/ja00527a032\n10.1039/D3SC00127J\n' > dois.txt
python3 $SKILL/download.py --file dois.txt --out ./papers && \
python3 $SKILL/rename.py --dir ./papers

# 单篇
python3 $SKILL/download.py 10.1021/ja00466a059 --out ./papers

# 重命名预览 / 强制重处理
python3 $SKILL/rename.py --dir ./papers --dry-run
python3 $SKILL/rename.py --dir ./papers --force
```

也可以直接对 AI 说"整理文献"、"给我这几个 DOI 下载并整理"，skill 会自动路由到这套流程。

## 已知行为

- Sci-Hub 对未收录 DOI（2021 年后新文献）返回 Verification 页而非 404 —— 属正常，自动走 OA 回退
- PMC 短时间重复请求会升级为 reCAPTCHA —— 冷却几分钟后恢复 PoW，重跑即可
- 镜像批量连打可能触发 IP 限流 —— 调大 `--sleep`（默认 1.5s）
- 关键词启发式可能丢核心概念 —— `SKILL.md` Phase 4 VERIFY 有人工复核项
