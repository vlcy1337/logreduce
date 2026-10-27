# LogReduce

LogReduce 是一个面向大日志目录的 Python 3.11+ CLI。它按配置顺序对每个
日志行执行筛选和正则替换，将处理结果合并成一个文件，并在完整成功后删除
已经被处理的源文件。

处理采用流式读取、规则单次编译、小文件批处理和超大文件分块并行。所有中间
结果位于日志目录内的隐藏事务目录；配置错误、解码失败、源文件发生变化或中
断都不会删除源日志。

## 安装

```bash
python3 -m pip install -e .
```

也可以不安装，直接在源码目录运行：

```bash
PYTHONPATH=src python3 -m logreduce --help
```

## 配置

```toml
version = 1
output = "combined.log"
encoding = "utf-8"

[[rules]]
name = "keep errors"
filter = { mode = "include", pattern = '(?i)\bERROR\b' }
replace = { pattern = 'trace_id=[0-9a-f]+', replacement = 'trace_id=<id>' }

[[rules]]
name = "drop health checks"
filter = { mode = "exclude", pattern = '/healthz' }
```

`rules` 严格按声明顺序执行。每个规则可以先执行 `include` 或 `exclude`
过滤，再执行全局正则替换；没有 `filter` 时只替换，没有 `replace` 时只过滤
过滤。替换字符串遵循 Python `re.sub` 规则，例如 `\1` 或 `\g<name>`。

正则只匹配行正文，不包含换行符。LF、CRLF、CR 和文件末尾是否有换行都会原
样保留。支持 Python 编解码器名称，默认使用严格 UTF-8。

`output` 必须位于日志目录内。输出已存在时默认拒绝运行；确认替换时使用
`--force`，旧输出会在事务中保留到新结果提交完成。

## 运行

```bash
logreduce --log-dir /var/log/application --config reduce.toml
```

常用选项：

```text
--workers N   指定工作进程数，默认使用当前 CPU 亲和集合
--force       事务式替换已有输出
--dry-run     校验配置、枚举文件，但不写入或删除
--verbose     在 stderr 输出进度和任务信息
```

工具递归选择日志目录中的所有普通文件，按相对路径的 Unicode 字典序处理，
因此最终内容顺序稳定。符号链接会被忽略；配置文件和输出文件不会被处理或
删除。成功提交后仅删除本次选中的普通源文件，空目录会保留。

## 安全模型

运行日志文件必须保持静态。提交前工具会再次检查每个源文件的 inode、大小和
纳秒级修改时间；任何变化都会中止本次运行。目录级 `flock` 防止同一日志目录
被两个 LogReduce 进程同时处理。

事务状态保存在 `.logreduce-txn-*` 中。正常处理错误会清理事务并保留源文件；
硬中断留下的事务会在下一次运行时根据状态完成回滚或清理。不要在运行期间
手动修改这些目录。

## 测试与基准

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 benchmarks/benchmark.py --size-mib 512
```

基准脚本会生成临时数据，将单条简单过滤与同机 `grep -E` 比较，并测量多进程
性能；大容量测试需要预留足够磁盘空间。
