# logReduce

`logReduce` 是一个用 Python 3 编写的高性能日志处理 CLI：

> 注意：默认模式会在成功合并后删除源文件。首次使用请先加 `--dry-run`，或使用 `--keep-sources` / `delete_sources = false` 验证结果。

- 按 TOML 配置中的有序规则，对日志目录中的文件逐行执行 `keep` / `drop` / `replace`。
- 默认原地处理每个文件（临时文件 + 原子替换），最后按顺序合并为一个文件。
- 合并成功后删除源文件；失败时不会误删。
- 全程按 `bytes` 流式处理，不强制 UTF-8 解码，二进制安全，保留 CRLF 和末尾无换行等细节。
- 纯标准库，零运行时依赖。

## 安装

要求 Python 3.11+。

```bash
pip install -e .
```

也可以不安装，直接从源码运行：

```bash
PYTHONPATH=src python3 -m logreduce --help
```

安装后会得到 `logreduce` 命令。

## 快速开始

准备日志目录和配置：

```bash
logreduce -d /var/log/app -c examples/logreduce.toml -o /backup/merged.log
```

常用选项：

```bash
logreduce -d ./logs -c logreduce.toml -o merged.log --mode direct --jobs 8
logreduce -d ./logs -c logreduce.toml --dry-run
logreduce --validate -c logreduce.toml
```

| 选项 | 说明 |
|---|---|
| `-d, --log-dir` | 日志目录，必填（`--validate` 除外） |
| `-c, --config` | TOML 配置文件，必填 |
| `-o, --output` | 输出文件，覆盖配置中的 `[output].path` |
| `--mode inplace\|direct` | 覆盖配置中的处理模式 |
| `--jobs N` | 并行进程数，`0` 表示自动 |
| `--dry-run` | 只展示将处理的文件，不修改任何文件 |
| `--keep-sources` | 合并后保留源文件 |
| `--force` | 允许覆盖已存在的输出文件 |
| `--no-fsync` | 跳过 fsync（更快，掉电时可能丢数据） |
| `--allow-changing` | 允许源文件在处理期间被其他进程修改 |
| `--stats` | 输出完整统计 |
| `--stats-json` | 以 JSON 输出统计 |
| `--quiet` | 不输出进度和摘要 |
| `--verbose` | 输出批次/分片进度 |
| `--validate` | 只校验配置和正则，不处理文件 |

退出码：

- `0`：成功
- `1`：运行错误（文件变化、输出已存在、删除失败等）
- `2`：配置或参数错误

## 配置格式

完整示例见 [`examples/logreduce.toml`](examples/logreduce.toml)。

```toml
version = 1

[input]
# 相对日志目录的 glob，支持 **、*、?、[...]
include = ["**/*.log"]
exclude = ["**/*.gz", "**/archive/**"]
follow_symlinks = false

[output]
# 输出文件；相对路径按当前工作目录解析
path = "merged.log"
# 仅当前一个文件末尾没有换行时插入
separator = "\n"
# 合并成功后删除源文件
delete_sources = true

[engine]
mode = "inplace"          # inplace | direct
jobs = 0                  # 0 = 自动
chunk_size = "8MiB"       # 读取块大小
fsync = true
sort = "path"             # path | mtime | none
allow_changing = false

[[rules]]
name = "丢弃调试行"
action = "drop"           # keep | drop | replace
match_type = "regex"      # literal | regex
pattern = '\b(DEBUG|TRACE)\b'
flags = "i"               # i m s x

[[rules]]
name = "脱敏错误行中的 token"
action = "replace"
match_type = "regex"
pattern = '(?i)(token=)[A-Za-z0-9._-]+'
replacement = '\1***'
when = "ERROR"            # 可选：仅处理满足条件的行
when_type = "literal"     # literal | regex
# when_flags = "i"

[[rules]]
name = "替换主机名"
action = "replace"
match_type = "literal"
pattern = "old-host"
replacement = "new-host"
```

### 规则语义

规则按配置数组顺序逐行执行：

- `keep`：只保留匹配行；不匹配行被丢弃，后续规则不再处理。
- `drop`：匹配行被丢弃。
- `replace`：替换匹配内容；支持正则捕获组 `\1`、`\g<name>`。
- `match_type` 默认 `regex`；`when_type` 默认 `literal`；`flags` 只对 regex 有效，可用 `i`/`m`/`s`/`x`。
- `when`：仅对 `replace` 有效，是额外的行级前置条件；只有条件满足时才执行替换。
- 多条 `keep` 是交集。
- 没有 `keep`/`drop` 时，所有行默认保留。
- 按行匹配：`^` / `$` 分别匹配每一行的开头/结尾；不支持跨行正则；字面量 pattern 不能包含换行。
- 行内容按 `\n` 切分；若行尾是 `\r\n`，匹配时去掉 `\r`，输出时原样恢复。
- 全程按 bytes 处理；`flags = "i"` 在 bytes 正则下只做 ASCII 大小写折叠。
- `replace` 的 replacement 使用 Python `re.sub` 模板语法。注意 `\x0a`、`\u000a` 不是合法的 replacement 转义；插入换行请用 `\n` 或 `\012`。

## 两种处理模式

### `inplace`（默认）

1. 每个文件写入同目录临时文件。
2. `fsync` 后 `os.replace` 原子替换源文件。
3. 所有文件处理完成后，按顺序合并到输出文件。
4. 输出落盘后删除源文件。

如果合并阶段失败，已处理的源文件保持处理后的内容，但不会删除源文件，也不会留下半成品输出文件。适合严格按“先原地处理、再合并”的语义使用。

### `direct`（更快）

1. 源文件只读。
2. 结果直接写入输出临时文件（大文件可自动按换行边界分片并行）。
3. 输出 `fsync` 并原子替换成功后，才删除源文件。

失败时源文件完全不变。需要最高吞吐或希望“全有或全无”时使用。

## 安全行为

- 输出文件已存在时默认拒绝覆盖，需显式 `--force`。
- 输出路径不能是目录、配置文件或日志目录本身；最终组件不能是符号链接；也不能与配置文件是硬链接。
- 输出文件会被自动排除在输入集合之外，支持输出放在日志目录内。注意：如果输出放在日志目录内，下一次用 `--force` 覆盖时不会把旧输出重新当作输入；需要保留历史内容时请使用新的输出路径。
- 配置文件本身也会被自动排除，即使 `include = ["**/*"]` 且配置放在日志目录内。
- 默认检测文件在扫描后是否被修改（大小/mtime）；检测到变化即中止，避免日志轮转/追加时丢数据。`--allow-changing` 可放行。
- `direct` 模式在删除源文件前会再次校验源文件未变化；`inplace` 模式会在原子替换前和删除前再次校验。
- 符号链接文件不会被处理（避免原地处理时替换链接而非目标）；`follow_symlinks` 只控制是否进入符号链接目录。
- 硬链接会按 inode 去重：内容只合并一次，成功后删除所有链接路径。
- 删除源文件前，输出文件已完成 `fsync` 和原子替换。
- `--dry-run` 不创建输出、不修改文件。

## 性能设计

- **bytes 流式处理**：大块读取（默认 8 MiB），不逐行解码。
- **字面量快路径**：`bytes.find` / `bytes.replace` 直接走 C 实现。
- **正则整块快路径**：当规则是纯 `replace` 且正则可证明只在本行内匹配时，直接对整块执行 `re.sub`，避免 Python 逐行循环；遇到 CRLF 自动回退严格行模式。
- **多进程**：按文件批次并行；当文件数不超过 worker 数且单文件很大时，自动按换行边界分片并行（`direct` 模式）。所有 worker 数上限为 256。
- **零拷贝合并优先**：使用 `os.copy_file_range` 做内核侧复制，失败时回退到 1 MiB 缓冲读写。
- **少量系统调用**：输出按 1 MiB 聚集；批量模式直接写大块，避免额外 join 拷贝。

参考基准（12 vCPU WSL2，`/tmp` tmpfs，243 MB / 300 万行，纯替换配置，实际结果取决于硬件和磁盘）：

| 配置 | 耗时 | 吞吐 |
|---|---:|---:|
| literal，jobs=1 | 1.05 s | 231 MiB/s |
| literal，jobs=8 | 0.57 s | 425 MiB/s |
| regex，jobs=1 | 2.15 s | 113 MiB/s |
| regex，jobs=8 | 0.98 s | 248 MiB/s |

运行自己的基准：

```bash
python3 bench/bench.py --lines 3000000
```

## 限制

- v1 只处理普通文件，不透明解压 `.gz` 等压缩日志。
- 只支持按行规则，不支持跨行正则。
- 符号链接文件会跳过；如需处理，请先复制/转换。
- 单行内容需要完整读入内存；极端情况下（整文件无换行）会占用与文件等量的内存。
- `inplace` 模式通过临时文件 + rename 替换，保留权限位，但不保留 ACL、xattr、属主或硬链接关系；`direct` 模式则完全不修改源文件，成功后删除。
- `flags = "i"` 对 bytes 正则只做 ASCII 大小写折叠。
- 并发写入检测依赖 size/mtime，极端竞态下仍可能漏检；处理活跃日志时建议先停止写入或使用 `--allow-changing` 明确接受风险。

## 开发与测试

```bash
python3 -m unittest discover -v
```

`tests/__init__.py` 会把 `src/` 加入 `sys.path`，因此无需先安装即可运行测试。

项目结构：

```
src/logreduce/
  cli.py        CLI 参数与流程编排
  config.py     TOML 解析与严格校验
  discovery.py  文件发现、glob、符号链接/硬链接处理
  rules.py      规则编译、行处理、整块快路径判定
  engine.py     流式引擎、原地/分片处理、多进程 worker
  merge.py      原子合并、零拷贝、源文件删除
  runner.py     批次划分、并行调度、分片任务
  models.py     输入文件/分片任务模型
  fsutil.py     原子写入、目录 fsync、copy_file_range
  stats.py      统计与输出
tests/          标准库 unittest 测试
bench/          合成日志基准脚本
examples/       示例配置
```
