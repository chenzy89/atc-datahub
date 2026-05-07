# ATC AFTN WebHub

AFTN（Aeronautical Fixed Telecommunication Network）报文实时接收、存储与 Web 查询系统。

## 功能特性

- **UDP 网络接收** — 监听组播/单播 UDP 报文，接收 AFTN 电报
- **多报文类型解析** — 支持 FPL（飞行计划）、DEP（起飞报）、ARR（落地报）、DLA（延误报）
- **SQLite 本地存储** — 原始报文与解析结果持久化存储，无需额外数据库服务
- **Web 查询界面** — 支持按航班号、起降机场、执飞日、应答机、机型等条件组合查询
- **REST API** — 提供 JSON API 接口，便于二次开发或对接其他系统

## 系统要求

- Python >= 3.8
- Linux / macOS / Windows（支持 Python 和 UDP 组播即可）

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置

编辑 `config.json`：

```json
{
  "system_name": "ATC AFTN WebHub",
  "network": {
    "aftn": {
      "bind_host": "0.0.0.0",
      "port": 31031,
      "multicast_group": "229.31.31.31",
      "interface_ip": "172.50.100.76"
    }
  },
  "database": {
    "path": "./data/aftn.db"
  },
  "web": {
    "host": "0.0.0.0",
    "port": 5000
  }
}
```

| 配置项 | 说明 |
|--------|------|
| `bind_host` | UDP 绑定地址，通常为 `0.0.0.0` |
| `port` | UDP 接收端口，默认 `31031` |
| `multicast_group` | 组播地址，留空则不加入组播 |
| `interface_ip` | 加入组播的网卡 IP，需根据实际网络填写 |
| `database.path` | SQLite 数据库文件路径 |
| `web.port` | Web 服务端口，默认 `5000` |

### 3. 启动

```bash
python3 -m aftn_web -c config.json --log-dir ./logs
```

按 `Ctrl+C` 终止。

**后台运行（建议配合 systemd）**：

```bash
nohup python3 -m aftn_web -c config.json --log-dir ./logs &
echo $! > /tmp/aftn_web.pid
# 终止：kill $(cat /tmp/aftn_web.pid)
```

### 4. 访问

- Web 页面：`http://<本机IP>:5000/`
- API 端点：`http://<本机IP>:5000/api/`

## 项目结构

```
atc_aftn_web/
├── aftn_web/
│   ├── __init__.py         # 包信息
│   ├── __main__.py         # 启动入口
│   ├── config.py            # 配置加载
│   ├── models.py            # 数据模型
│   ├── database.py          # SQLite 数据库层
│   ├── parser.py            # AFTN 报文解析器
│   ├── receiver.py          # UDP 接收器
│   ├── webapp.py            # Flask Web 应用
│   └── templates/
│       └── index.html       # 前端查询页面
├── config.json              # 配置文件
├── requirements.txt         # Python 依赖
├── pyproject.toml           # 项目元数据
└── README.md                # 本文档
```

## API 接口

### 统计信息

```
GET /api/stats
```

返回示例：

```json
{
  "total_flight_plans": 120,
  "by_type": { "FPL": 80, "DEP": 25, "ARR": 14, "DLA": 1 }
}
```

### 查询飞行计划

```
GET /api/flight_plans
```

支持以下查询参数（均为可选）：

| 参数 | 说明 |
|------|------|
| `callsign` | 航班号（模糊匹配） |
| `adep` | 起飞机场四字码（模糊匹配） |
| `adest` | 目的地机场四字码（模糊匹配） |
| `dof` | 执飞日期（ISO 格式，如 `2026-05-07`） |
| `ssr` | 应答机码（模糊匹配） |
| `aircraft_type` | 机型（模糊匹配） |
| `source_message_type` | 报文类型：FPL / DEP / ARR / DLA |
| `keyword` | 综合关键词（匹配航班号/机场/航路） |
| `limit` | 每页条数（默认 100，最大 500） |
| `offset` | 分页偏移 |

返回示例：

```json
{
  "total": 1,
  "records": [
    {
      "id": 1,
      "callsign": "CSN101",
      "adep": "ZGOW",
      "adest": "ZGGG",
      "ssr": "A2431",
      "aircraft_type": "B738",
      "flight_rules": "I",
      "route": "A461 IDUPA W22 GLN",
      "dof": "2026-05-07",
      "etd": "2026-05-07 13:30:00",
      "atd": "2026-05-07 13:45:00",
      "eta": "2026-05-07 14:20:00",
      "ata": "2026-05-07 14:18:00",
      "source_message_type": "ARR",
      "raw_message_text": "(ARR-CSN101-...)",
      "created_at": "2026-05-07 06:00:00",
      "updated_at": "2026-05-07 06:30:00"
    }
  ]
}
```

### 飞行计划详情

```
GET /api/flight_plans/<id>
```

### 查询原始 AFTN 报文

```
GET /api/aftn_messages?message_type=FPL&limit=50
```

## AFTN 报文格式

### 支持的报文类型

系统支持解析以下 ICAO 标准 AFTN 报文：

| 类型 | 说明 | 示例 |
|------|------|------|
| FPL | 飞行计划 | `(FPL-CSN101-IS-B738/M-SDE2E3FGHIJ5J15WXY/LB1-ZGOW1330-N0480F300 A461 IDUPA W22 GLN-ZGGG0020-REG/B5600 DOF/260507)` |
| DEP | 起飞报 | `(DEP-CSN101-ZGOW1203-ZGGG)` |
| ARR | 落地报 | `(ARR-CSN101-ZGOW-ZGGG1458)` |
| DLA | 延误报 | `(DLA-CSN101-ZGOW1500-ZGGG)` |

### 接收数据格式

系统同时支持以下两种输入格式：

**格式一：纯 AFTN 文本**

直接在 UDP 包 payload 中发送括号包围的报文：

```
(FPL-CSN101-IS-...)
```

**格式二：JSON 包裹（推荐）**

通过组播/单播发送 JSON 格式数据：

```json
{
  "UtcTime": "2026-05-07 03:56:35",
  "MessageType": "",
  "MessageText": "RDX2627 070356 FF ZGJDADTM ... (FPL-CSN101-ZGOW1330-...)"
}
```

`MessageText` 中的 AFTN 报文会被自动提取和解析。

## 数据库

使用 SQLite 存储，数据文件路径由 `config.json` 中的 `database.path` 指定。

### 主要表结构

**aftn_messages** — 原始 AFTN 报文

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER | 主键 |
| raw_text | TEXT | 原始报文全文 |
| message_type | TEXT | 报文类型 |
| message_text | TEXT | 提取的核心报文内容 |
| utc_time | TEXT | 报文 UTC 时间 |
| received_at | TEXT | 接收时间 |

**flight_plans** — 解析后的飞行计划

| 字段 | 类型 | 说明 |
|------|------|
| id | INTEGER | 主键 |
| callsign | TEXT | 航班号 |
| adep | TEXT | 起飞机场 |
| adest | TEXT | 目的地机场 |
| ssr | TEXT | 应答机码 |
| aircraft_type | TEXT | 机型 |
| flight_rules | TEXT | 飞行规则（I/V） |
| route | TEXT | 航路信息 |
| dof | TEXT | 执飞日期 |
| etd / atd | TEXT | 预计/实际起飞时间 |
| eta / ata | TEXT | 预计/实际落地时间 |
| raw_message_text | TEXT | 原始报文全文 |

## 开机自启（systemd）

创建服务文件 `/etc/systemd/system/atc-aftn-web.service`：

```ini
[Unit]
Description=ATC AFTN WebHub
After=network.target

[Service]
Type=simple
User=<你的用户名>
WorkingDirectory=/home/share/atc_aftn_web
ExecStart=/home/<用户名>/.pyenv/shims/python3 -m aftn_web -c /home/share/atc_aftn_web/config.json --log-dir /home/share/atc_aftn_web/logs
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

启用服务：

```bash
sudo systemctl daemon-reload
sudo systemctl enable atc-aftn-web
sudo systemctl start atc-aftn-web
sudo systemctl status atc-aftn-web
```

## License

MIT
