# data-qd-164 · 受约束赛事赛程生成

Round-robin fixture schedules with venue and team availability constraints。业务输入、计算和当前边界详见 [数据契约](docs/contract.md)。

## 安装与演示

Python >=3.11，运行和测试仅用标准库；建议 Python 3.12。使用 uv 建立独立环境，构建工具锁版本。首次安装无公共缓存时需要访问包索引；业务运行不访问网络。

```sh
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements-build.lock
uv pip install --python .venv/bin/python --no-build-isolation --no-deps .
uv pip check --python .venv/bin/python
.venv/bin/fixture-scheduler --source fixtures/input.json --output run/report.json
.venv/bin/python -m unittest discover -s tests -v
```

修改源码后重新安装。样例应生成 `round_count=3` 和 `fixture_count=6`，所有队伍对各出现一次。本仓不共享其他仓环境，不依赖Docker、外部账号或持续服务。

## 最小扰动修复

`fixture-repair`（或 `python -m fixture_scheduler.repair`）读取既有完整赛程及更新后的可用性、逐比赛场馆许可和锁定承诺，输出满足全部约束的最小扰动修复方案。它接受同样的 `--source` 与 `--output`，复用同一受限JSON读取和原子报告发布；业务运行仅用标准库。

```sh
.venv/bin/fixture-repair --source fixtures/repair_input.json --output run/repair.json
```

目标按优先级最小化改日期轮数、改场馆场数，最后取按轮号展开的规范下标向量；无解时正常退出0并发布 `feasible=false` 的完整报告。字段与校验细节见 [数据契约](docs/contract.md)。

## 官方赛果裁定与同分积分榜

`fixture-standings`（或 `python -m fixture_scheduler.standings`）读取一个自足的官方赛果修订与队伍裁定批次，按两个知识序号截止点分别结算积分榜、逐场有效性依据和名次变化。它复用同一受限JSON读取与原子报告发布，接受同样的 `--source` 与 `--output`，业务运行仅用标准库，不读取日期、场馆、旧赛程或锁。

```sh
.venv/bin/fixture-standings --source fixtures/standings_input.json --output run/standings.json
.venv/bin/fixture-standings --source fixtures/standings_withdrawal_input.json --output run/standings_withdrawal.json
```

样例 `fixtures/standings_input.json` 取 teams 顺序 `[A, C, B, D]`、cutoffs `[7, 8]`：截止 7 四队同为净3分，小循环递归拆分后榜单为 `[A, B, C, D]`；截止 8 的更正使 B、C 那场作废，榜单变为 `[A, C, D, B]`。`fixtures/standings_withdrawal_input.json` 展示退赛、恢复与扣分。字段、排序定义与实现取舍见 [数据契约](docs/contract.md)。

两份积分榜各自从冻结的可见记录视图独立结算，更正或裁定不会跨截止点或跨同分组残留。

## 球票库存与整数退款账本

`fixture-tickets`（或 `python -m fixture_scheduler.tickets`）在独立持久 SQLite 文件中维护球票业务账本：以 `circle_rounds` 的有向比赛身份初始化场次票额与单价，原子接收跨场购买订单和部分退票，并给出可售库存、逐票实付与已收/已退款守恒报表。它复用同一受限 JSON 读取与原子报告发布，仅用标准库（含 `sqlite3`），不接支付、网络或外部数据库，也不读写另外三个入口的数据。

```sh
fixture-tickets init   --db run/tickets.db --source fixtures/tickets_catalog.json
fixture-tickets apply  --db run/tickets.db --source fixtures/tickets_batch_b1.json
fixture-tickets apply  --db run/tickets.db --source fixtures/tickets_batch_b2.json
fixture-tickets report --db run/tickets.db --output run/tickets_report.json
```

三个子命令 `init` / `apply` / `report` 也可由测试直接调用 `fixture_scheduler.tickets` 的 `init_ledger`、`apply_batch`、`build_report` / `write_report`。DB 是独立 SQLite 文件，跨进程保留；任何已有文件或符号链接都拒绝覆盖，非法目录不会留下半初始化数据库。

## 官方赛果驱动的球票退款对账

第四个子命令 `sync-refunds`（仍由 `fixture_scheduler.tickets` 入口提供）读取一份 `fixture-standings` 报告，按唯一选中截止快照的 `match_audit` 把官方裁定为 `void` 或 `withdrawn` 场次中尚未退款的票按原 `paid_cents` 退回；`pending` 与 `counted` 不退，已退款票跳过，后续改判 `counted` 也不撤销历史退款。仅用标准库，不接支付网络或外部服务。

```sh
fixture-tickets apply        --db run/tickets.db --source fixtures/tickets_sync_setup.json
fixture-tickets apply        --db run/tickets.db --source fixtures/tickets_sync_prerefund.json
fixture-tickets sync-refunds --db run/tickets.db --source fixtures/standings_refunds_report.json \
    --cutoff 6 --batch-id SYNC-1 --output run/sync_receipt.json
```

它接受 `--db --source --cutoff --batch-id --output`。cutoff 必须是非 bool 整数且恰好匹配一个 snapshot；快照的有向 `(round, home, away)` 键集合必须与账本场次完全一致，reason 仅允许 `pending`/`void`/`withdrawn`/`counted`。退款按 `order_id` 分组、`ticket_no` 升序生成规范操作，每单 `refund_id` 为 `frs-` 加 SHA-256，摘要输入绑定 `batch_id`、source 原始字节哈希、十进制 cutoff、订单与所退票号，超过 1000 个操作或 20000 张票整批拒绝。相同批次与规范请求重放返回原回执不重复退款；同 `batch_id` 但 source 哈希或 cutoff 变化、或任何不同规范请求复用批次都被 `apply_batch` 拒绝且账本不变；无可退票据时返回 `status=noop`、不写库、不占用批次。字段、幂等与冲突语义见 [数据契约](docs/contract.md)。

自制样例 `fixtures/standings_refunds_input.json` 取四队、cutoffs `[6, 10]`：截止 6 时 `(2,C,A)` 为 `void`、两场涉及 B 的比赛因 B 退赛为 `withdrawn`、一场 `counted`、两场 `pending`；截止 10 的更正把 `(2,C,A)` 重新裁定为 `counted` 并恢复 B。配套 `tickets_sync_setup.json` 与 `tickets_sync_prerefund.json` 构造出含一张已退票的 `void` 场次（另两张未退）、一个 `withdrawn` 场次和一个 `counted` 场次，`standings_refunds_report.json` 是由 `fixture-standings` 生成的对应报告信封。


自制样例 `fixtures/tickets_catalog.json` 取四队 circle 赛程，前两场容量均 1、单价均 1 分。`tickets_batch_b1.json` 是契约规定的手算联动样例：订单 O 跨两场各买 1 张并给 1 分优惠，最大余数法把优惠分给票 1，净额为 `[0, 1]`；随后退掉票 1（零实付退票仍释放票额），新订单 P 无优惠买回该场。此时两场 active 均 1、available 均 0，全库 `paid=2, refunded=0, retained=2`。`tickets_batch_b2.json` 再退 O 的票 2，全库变为 `refunded=1, retained=1`，仅第二场 available 为 1；之后重试 B1 只返回原冻结回执，不再扣票收款。字段、整数分摊、幂等与并发语义见 [数据契约](docs/contract.md)。

输入JSON最多8MB，拒绝重复键、非有限数值和未定义字段；源字节SHA256随结果保留。
输出是可独立读取的JSON；成功后原子替换，输入/计算失败退出2并保留旧报告。
输入和输出不得相同，输出符号链接拒绝。单操作者批处理，不承诺并发发布或断电恢复。
同输入重跑不累加。测试使用临时目录，运行产物在忽略的run/。

src/fixture_scheduler/model.py 为业务模型，common.py 提供受限JSON及报告发布，
cli.py 衔接数据链路。fixtures 为明确自制样例，代码与样例采用MIT许可。
