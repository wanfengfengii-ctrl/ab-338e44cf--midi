# MIDI Timeline Normalizer

数字音乐档案馆使用的标准 MIDI 文件（SMF）时间轴归一化服务。它把各轨道的
通道事件换算到同一条微秒时间轴，消除多轨合并与速度变化造成的时刻漂移。

## 接口

### `POST /api/midi/normalize`

- 请求体：原始 MIDI 字节，最大 **1 MiB**。
- 仅支持：
  - SMF 格式 **0** 与 **1**；
  - 正的 **PPQN**（SMPTE/时间码分频被拒绝）；
  - 轨道数与通道事件数合计不超过 **10 000**。
- 严格校验：文件块（chunk）、变长整数（≤4 字节）、运行状态、事件长度、
  截断数据、非法状态字节、缺失/重复 End Of Track 以及尾部未声明字节。
- 速度：初始 `500000 µs/四分音符`；格式 1 只采用首轨的速度事件，其它轨道
  出现速度事件会被拒绝。速度自其所在 tick 起生效。
- 时刻以**最简微秒分数**（`Fraction` 约分后的分子/分母）精确返回。

成功响应中的事件按 `(tick, track, index)` 稳定排序：

```json
{
  "format": 1,
  "ppqn": 480,
  "event_count": 1,
  "events": [
    {
      "tick": 240,
      "track": 1,
      "index": 0,
      "channel": 0,
      "type": "note_on",
      "status": "0x9",
      "params": {"note": 60, "velocity": 100},
      "raw": "903c64",
      "time": {"microseconds": "250000/1", "numerator": 250000, "denominator": 1}
    }
  ]
}
```

结构错误返回 HTTP 400，并给出可定位的**绝对字节偏移**，且绝不返回部分
时间轴：

```json
{"error": "truncated data while reading channel event data byte", "offset": 25}
```

`GET /health` 用于容器健康检查。

## 本地运行（无第三方运行时依赖，仅需 Python 3.11+）

```bash
python -m app.server            # 默认 0.0.0.0:8000，可用 PORT 覆盖
curl -s --data-binary @song.mid http://127.0.0.1:8000/api/midi/normalize
```

## Docker / Compose

```bash
docker compose up -d api                 # 宿主机端口可用 HOST_PORT 配置（默认 8080）
HOST_PORT=9000 docker compose up -d api

# 一次性验证：待 api 健康后运行编译检查、全部测试、变速多轨 API 冒烟，
# 以退出码报告结论：
docker compose run --rm verify
```

`verify` 服务通过 `depends_on: condition: service_healthy` 保证只在 API
健康后启动；`scripts/verify.sh` 依次执行：

1. `compileall` 构建/语法检查；
2. `pytest` 单元与接口测试；
3. `scripts/smoke_test.py` 对含多次变速的格式 1 多轨文件做端到端冒烟。

## 开发

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q
```
