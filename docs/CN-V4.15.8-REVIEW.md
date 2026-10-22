# v4.15.8 同步与 CN 复核

上游来源为 `ProtossBlaster/leapmotor-mate` 的 `v4.15.8`，提交 `9d82432`，通过合并提交保留上游历史。此前的 T03 具名车况修正保存在 `e3719b1`，同步后继续生效。

## 上游改动与中国区接入

| 范围 | 复核结果 |
|---|---|
| v4.15.5 电池页视图、充电曲线刷新、重启后的车外温度 | 保留上游实现；CN 的采样和入库仍使用共用流程 |
| v4.15.6 MQTT 充电枪传感器 | 把上游新增属性接入共用 `VehicleData`；EU 使用端口判断，CN 继续按 `1149` 解析，未知值向 MQTT/EVCC 发布未知 |
| 首页车图 | EU 保留上游先显示静态图再加载动画的行为；CN 保留每账号、每 VIN 的官方整车图片缓存与 `vehicle_id` 绑定 |
| v4.15.7 合并充电的电池健康估算 | 保留上游对子充电分段及边界的修正；CN 充电状态和电流语义不变 |
| v4.15.8 增程电池选项 | EU 保留上游完整电池选项；CN 短信设置仍要求逐车填写实际可用容量，不把国际版容量视为中国区配置 |
| v4.15.8 云端回应诊断 | CN 实际 HTTP 请求也写入有上限的回应记录；包含时间、方法、无查询参数的路径、HTTP 状态和数值业务码，不保存请求头、正文、VIN 参数或 Token |
| CN 短信及续期 | 保留账号与车端两层加密登录状态、跨进程锁、免短信续期、风控冷却及不自动重发车控的规则 |
| CN 共享权限与遥测 | 保留能力、指令权限及位置权限；GPS 继续从 GCJ02 转换到 WGS84，未知读数不补零 |
| CN 轮询 | 保留行驶默认 6 秒、设置范围 6–60 秒、一次迁移及约一分钟停车确认 |
| T03 | 保留具名/数字车况、时间、布尔值、CN 充电状态及无法解析时仍能导出原始信号的诊断流程 |
| 健康充电局部刷新 | 控件仍在 `#cmd-grid` 内，整组随 `outerHTML` 刷新替换 |

## 简体中文

- 新增 `zh-CN` 完整词库及保养页词条，保留格式占位符、HTML 标签和日期数据。
- 新安装的 CN 版本默认简体中文；已经保存的语言选择保持不变。
- 语言选择沿用原版写法：`🇨🇳 {{ t('lang_zh_cn') }}`、`🇭🇰 {{ t('lang_zh_tw') }}`；设置页与 CN 短信设置页一致。
- 短信设置、官方每日能耗、健康充电、未开放功能提示和 CN 公开错误信息均支持简繁体。
- 切换短信设置页语言时保留手机号、验证码、容量和 PIN，不额外发送短信或车控。
- 数值和人民币价格使用小数点；添加语言不会改变货币、时区、单位或车况协议。
- 翻译为静态文件；运行镜像不需要新增文字转换依赖。

## 验证

使用合成账号、假传输和临时数据库，未登录真实车辆，也未发送车控。测试框架阻挡外部网络。浏览器用例覆盖手机/桌面宽度、简繁切换、字段保留及语言保存。检查命令：

2026-10-22 验证结果：

- 相关回归：252 项通过。
- 全量：5942 项通过、2 项跳过。唯一警告来自故意构造重复 ZIP 成员的拒绝测试。
- Docker 最终映像建置成功，版本为 4.15.8。
- 使用 `--network none` 与临时 `/data` 启动最终映像：健康检查 200，新安装默认简体，繁体选择可保存并在重新请求设置页后保留，旗帜标签及简体错误信息正确；车况表仍为 0 行。验证容器随后删除。

可复核命令：

```sh
PLAYWRIGHT_BROWSERS_PATH=/tmp/mate-t03-playwright .venv/bin/python -m pytest -q \
  tests/test_cn_language.py tests/test_translations_complete.py \
  tests/test_cn_adapter.py tests/test_cn_t03.py tests/test_cn_upstream_4158.py \
  tests/test_cn_token_refresh.py tests/test_cn_car_picture.py tests/test_cn_maps.py \
  tests/test_cn_six_second_polling.py
PLAYWRIGHT_BROWSERS_PATH=/tmp/mate-t03-playwright .venv/bin/python -m pytest -q -n 4 --dist worksteal
docker build -t leapmotor-mate-cn:v4.15.8-review .
```

T03 仍需车友按实际年款对照官方 App 验证。新诊断保留无法解析的脱敏信号，有利于继续定位车型差异。
