<p align="center">
  <a href="https://nace.ai"><picture><source media="(prefers-color-scheme: dark)" srcset="docs/assets/nace-logo-white.svg"><img src="docs/assets/nace-logo-black.svg" alt="Nace" height="40"></picture></a>
</p>

<h1 align="center">Drex for ClickHouse®</h1>

<p align="center"><b>Classify text and score rows directly from ClickHouse SQL.</b></p>

<p align="center">
  <a href="https://github.com/nace-ai/drex-clickhouse/actions/workflows/MockTests.yml"><img src="https://img.shields.io/badge/tests-GitHub_Actions-2088FF" alt="Tests"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT license"></a>
</p>

Ask typed questions about your data directly from ClickHouse SQL. Five executable functions connect ClickHouse to
[Drex](https://console.nace.ai/docs) to classify text, score rows, and return calibrated probabilities as native
ClickHouse values.

Use them for support-ticket routing, document classification, and scoring rows you already keep in ClickHouse. The
functions run on self-hosted ClickHouse (a Python script ClickHouse runs as an executable UDF) and on ClickHouse
Cloud (the same script, uploaded as a Python UDF). They need ClickHouse 26.7 or later: earlier versions pass
arguments to executable UDFs in the types they have rather than the declared ones.

[Getting started](#getting-started) · [Examples](#examples) · [API reference](docs/API.md) ·
[ClickHouse Cloud](#clickhouse-cloud)

## What it does

| Function | Result | Use case |
| --- | --- | --- |
| `drex_noul` | `Nullable(Float64)` probability of yes | Flag refund requests |
| `drex_choice` | Named tuple: label, probabilities, confidence | Route tickets to a team |
| `drex_score` | Named tuple: score, probabilities, confidence | Rank urgency on an ordered scale |
| `drex_decide` | JSON text of the full response | Ask several questions per row |
| `drex_decide_image` | JSON text about image bytes | Classify photos or receipts |

## Getting started

Requires Docker with Compose for the container setup. The credential prompts below use Bash.

### 1. Get an API key

Make one in the [Nace console](https://console.nace.ai). The functions read it from a file on the server, readable
only by the `clickhouse` user; it never appears in SQL.

### 2. Run ClickHouse with the functions

The quickest way is the Docker image in this repository: the official `clickhouse/clickhouse-server` image with a
Python environment holding [`nace-sdk`](https://pypi.org/project/nace-sdk/), the script, and the functions' config.

```bash
git clone https://github.com/nace-ai/drex-clickhouse.git
cd drex-clickhouse
mkdir -p secrets
# Paste the key at the hidden prompt, so it stays out of your shell history
(umask 077 && read -rs -p 'Nace API key: ' key && printf '%s' "$key" > secrets/nace_api_key); echo
sudo chown 101:101 secrets/nace_api_key  # Linux: the clickhouse user in the container
docker compose up -d --build --wait
docker compose exec clickhouse clickhouse-client
```

The server listens on `127.0.0.1` only, as the image's passwordless `default` user. Add users and passwords before
exposing it.

### Or add the functions to an existing server

From a clone of this repository, on a Linux host running `clickhouse-server` 26.7 or later (Python 3.10 or later;
on Debian and Ubuntu, `python3-venv` too):

```bash
sudo mkdir -p /opt/drex-clickhouse /etc/clickhouse-server/drex
sudo python3 -m venv /opt/drex-clickhouse/venv
sudo /opt/drex-clickhouse/venv/bin/pip install -r udf/requirements.txt
sudo cp udf/drex_udf.py /opt/drex-clickhouse/
sudo cp config/drex_function.xml /etc/clickhouse-server/
# The key, readable only by the clickhouse user
sudo sh -c 'umask 077 && cat > /etc/clickhouse-server/drex/nace_api_key'   # paste the key, then Ctrl-D
sudo chown clickhouse:clickhouse /etc/clickhouse-server/drex/nace_api_key
```

Then run `SYSTEM RELOAD FUNCTIONS` in `clickhouse-client` (or restart the server). ClickHouse loads
`*_function.xml` from its config directory by default (`user_defined_executable_functions_config`); with another
setting, put the file where it looks. `config/drex_function.xml` names the paths above: change them there if you
install elsewhere. On a cluster, install on every replica.

### 3. Query

```sql
CREATE TABLE tickets (id UInt32, body String) ENGINE = MergeTree ORDER BY id;
INSERT INTO tickets VALUES
    (1, 'I was charged twice. Please refund the duplicate payment.'),
    (2, 'How do I change my email address?');

CREATE TABLE ticket_answers ENGINE = MergeTree ORDER BY id AS
SELECT id, drex_noul(body, 'Is the customer asking for a refund?', '', '') AS refund_probability
FROM tickets;

SELECT * FROM ticket_answers WHERE refund_probability > 0.8;
```

Answers come from the API and can vary. Saving them in a table lets you query the results without sending the same
rows again.

## Examples

### Classify a row

```sql
SELECT id, drex_choice(body, 'Which team should handle this ticket?', ['billing', 'technical', 'account'], '') AS team
FROM tickets;
```

The result is a named tuple: `team.choice`, `team.probabilities` (each label with its probability, sorted by label),
and `team.confidence`.

### Score on a scale

```sql
SELECT id, drex_score(body, 'How upset is the customer?', ['calm', 'annoyed', 'angry'], '').score AS upset
FROM tickets;
```

### Ask several questions in one request per row

```sql
SELECT id, JSONExtract(answers, 'answers', 'refund', 'noul', 'Float64') AS refund, answers
FROM (
    SELECT id, drex_decide(
        toJSONString(map('ticket', body)),
        '{"refund": {"type": "noul", "instructions": "Is the customer asking for a refund?"},
          "team": {"type": "choice", "instructions": "Which team?",
                   "criteria": {"billing": "Payments and refunds", "technical": "Bugs and outages"}}}',
        ''
    ) AS answers
    FROM tickets
);
```

`drex_decide` returns Drex's whole response as JSON text. It also takes labels with descriptions, which the typed
functions don't.

### Images

`file()` reads from the server's `user_files` directory. With the Docker setup, copy a folder of PNG files there
first:

```bash
docker compose cp ./photos clickhouse:/var/lib/clickhouse/user_files/photos
```

```sql
SELECT _file, drex_decide_image(
    '"A product photo"',
    [_contents],
    '{"damaged": {"type": "noul", "instructions": "Is the product damaged?"}}',
    ''
) AS answer
FROM file('photos/*.png', 'RawBLOB', '_contents String');
```

`drex_decide_image` sends 1 to 8 PNG, JPEG or WebP images per row, read as bytes by ClickHouse, to `drex-max` by
default. Drex never fetches a URL. See the [API reference](docs/API.md) for every argument, result and error.

## Requests and cost

Every function sends each non-NULL row to the configured API: the input, the instructions and the labels. Use data
you are authorized to send to that service. A custom base URL receives both the API key and the rows; HTTPS is
required except to `localhost`.

Each evaluated non-NULL row normally makes one billed request. Retries add attempts, but only an answered (200)
request is billed, so a retry is billed twice only if a 200 never reached the server. ClickHouse evaluates the
function a block at a time, which decides what is sent:

- **A whole block is sent.** A `WHERE` on an answer, or `ORDER BY ... LIMIT`, evaluates every row it reads. Filter
  and limit the input rows in a subquery first, then call the function.
- **A cancelled query still finishes its block.** `KILL QUERY` (or Ctrl-C) waits until the function has answered
  every row of the block it was given, and those requests are billed. `SETTINGS max_block_size = 1000` bounds how
  many rows that can be, and lets a large table's blocks run in parallel across the pool.
- **A repeated query sends its rows again**, and so does reading a view twice. A constant call reused under an alias
  (`drex_noul('text', ...) AS a, a`) can run twice. Save answers with `CREATE TABLE ... AS SELECT` or
  `INSERT INTO ... SELECT` to reuse them.

## Configuration and behavior

Text and JSON calls default to `drex-v2.0`; image calls default to `drex-max`. Every SQL argument is required:
pass `''` for optional strings. Row-level failures return NULL (or a tuple whose answer field is NULL); invalid
questions, authentication failures and unknown models fail the query. Requests get at most five attempts within
a 120-second retry window. See [errors and configuration](docs/API.md#errors) for precise conditions and options.

Each function's processes read the key file when they start and keep the key. After changing it, run
`SYSTEM RELOAD FUNCTIONS` so the next query starts them with the new key.

### Concurrency

Each function is a pool of `pool_size` processes per server (2 in `config/drex_function.xml`), each with at most
`--concurrency` requests in flight (4). One block runs in one process, and one query uses at most `max_threads`
processes, so at most `pool_size × concurrency` requests per function and server are in flight: 8 by default, a free
account's limit. A paid account allows 16; on a cluster, divide the budget across replicas. Drex's rate-limit answers
(429) are retried after the wait Drex asks for, so going over the limit costs time, not answers.

## ClickHouse Cloud

ClickHouse Cloud runs Python executable UDFs with network access. The service must run ClickHouse 26.7 or later
(`SELECT version()`); a service on an older release can move to the fast release channel in its settings. Each
function is uploaded as a zip with the same script as `main.py`, a `requirements.txt`, and a `config.json`:

```bash
read -rs -p 'Nace API key: ' NACE_API_KEY && export NACE_API_KEY; echo
python3 cloud/build_bundles.py --pool-size 2   # writes cloud/dist/drex_<function>.zip
```

**The key is inside the zips.** ClickHouse Cloud has no secrets store for UDFs yet. Make a key for these functions
alone, keep the zips out of version control, rotate the key when someone who can manage the service's UDFs leaves,
and move it to the secrets store when ClickHouse ships one.

Each process on ClickHouse Cloud may start only 5 threads, so it sends at most 4 requests at once. A pool of 2
processes with 4 each reaches 8 in flight per replica, and a query that reads one stream, which one process answers,
still gets 4.

Then create the functions with Terraform (`cloud/terraform/main.tf`, ClickHouse provider 3.24.0 or later), with an
organization API key that has the Admin role:

```bash
export CLICKHOUSE_ORG_ID='YOUR_ORGANIZATION_ID'
read -rs -p 'Key ID: ' CLICKHOUSE_CLOUD_API_KEY && export CLICKHOUSE_CLOUD_API_KEY; echo
read -rs -p 'Key secret: ' CLICKHOUSE_CLOUD_API_SECRET && export CLICKHOUSE_CLOUD_API_SECRET; echo
cd cloud/terraform
terraform init
terraform plan -var='service_id=YOUR_SERVICE_ID' -out=tfplan
terraform apply tfplan
```

Or create each one in the console (or through the Cloud API) with these settings:

| Setting | Value |
| --- | --- |
| Name | `drex_noul`, `drex_choice`, `drex_score`, `drex_decide`, `drex_decide_image` |
| Runtime | Python 3.11, network access on |
| Type | Executable pool, pool size 2 (the `--pool-size` the zips were built with) |
| Format | `RowBinary`, with the chunk header sent |
| Arguments and return type | as in [the API reference](docs/API.md) and `config/drex_function.xml` |
| Command read timeout | 200000 ms (a slow row can take up to about 180 s) |
| Max command execution time | 3600 s |

Function names are unique within a ClickHouse Cloud organization: one set of these functions, with one key, serves
every service it is attached to. Attaching the first UDF to a service restarts its replicas one by one.

## Development

`udf/drex_udf.py` is the whole integration, one script on [`nace-sdk`](https://pypi.org/project/nace-sdk/).
`tests/mock/` is drex-duckdb's mock of Drex's API (`drex_mock.py`), copied unchanged.

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/python -m pytest                 # the script, the Cloud bundles, the mock; ClickHouse tests skip

# The functions through ClickHouse, against the mock
python3 tests/clickhouse_functions.py tests/.generated/drex_test_function.xml
docker compose -f docker-compose.yml -f tests/docker-compose.test.yml up -d --build --wait
DREX_CLICKHOUSE_URL=http://127.0.0.1:8123 DREX_MOCK_URL=http://127.0.0.1:8787 .venv/bin/python -m pytest tests/test_clickhouse.py
```

`CLICKHOUSE_VERSION` picks the server image (26.8.36.3 by default). `.github/workflows/LiveTests.yml` runs
`tests/live` against the production API on `workflow_dispatch`, with billed requests.

## Support and contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for local checks and pull requests, and the
[API reference](docs/API.md) for the complete contract. Report bugs through
[GitHub Issues](https://github.com/nace-ai/drex-clickhouse/issues), with your ClickHouse version, whether it's
ClickHouse Cloud, and a minimal example using synthetic data. Remove keys and private data from reports.

## License and trademarks

[MIT](LICENSE). Nace artwork belongs to Nace AI. The software license does not grant trademark rights.
See [asset sources and branding guidance](docs/assets/README.md).

This integration is maintained by Nace AI. References to the platform describe compatibility,
not sponsorship or endorsement by its owner.

ClickHouse is a registered trademark of ClickHouse, Inc. [clickhouse.com](https://clickhouse.com).
