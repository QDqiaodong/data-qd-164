本目录为原创虚构合成数据，使用MIT许可。没有真实个人、线上实验、生产日志、交易、项目进度或搜索用户信息；不来自外部平台。

`tickets_catalog.json`、`tickets_batch_b1.json`、`tickets_batch_b2.json` 同为虚构球票账本样例：队名、票额、单价（分）、订单与退票标识均为手工构造，仅用于演示整数优惠分摊、部分退票与批次重试，不代表任何真实票务交易。

`tickets_sync_setup.json`、`tickets_sync_prerefund.json` 与 `standings_refunds_input.json` / `standings_refunds_report.json` 为官方赛果驱动退款对账的联动虚构样例：前者在同一账本里布置 void/withdrawn/counted 场次的已售与已退票，后两者是手工构造的积分榜输入及由 `fixture-standings` 生成的报告信封，全部标识与金额均为合成数据。

`evidence_hand_example.json` 及同前缀的 `evidence_uncovered.json`、`evidence_over_budget.json`、`evidence_interior_gap.json`、`evidence_shared_window.json`、`evidence_inconsistent.json` 是赛后录像取证包入口的虚构样例：球队、场馆、摄录机、时间秒数与文件字节数均为手工构造，赛程信封由同一四队 circle 赛程整理而成，不含任何真实录像、真实时钟或真实文件大小。
