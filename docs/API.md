# SQL API

Install the functions as shown in the [README](../README.md#getting-started), on ClickHouse® 26.7 or later.
ClickHouse executable functions take a fixed number of arguments, so every argument is required. Pass `''` (or
`NULL`) for an optional one: no criteria, no instructions, or the default model.

| Function | Result |
| --- | --- |
| `drex_noul(input, instructions, criteria, model)` | `Nullable(Float64)`: the probability that the answer is true |
| `drex_choice(input, instructions, labels, model)` | `Tuple(choice Nullable(String), probabilities Array(Tuple(value String, probability Float64)), confidence Nullable(Float64))` |
| `drex_score(input, instructions, levels, model)` | `Tuple(score Nullable(Float64), probabilities Array(Tuple(index UInt32, value String, probability Float64)), confidence Nullable(Float64))` |
| `drex_decide(state, questions, model)` | `Nullable(String)`: Drex's response body as JSON text |
| `drex_decide_image(state, images, questions, model)` | `Nullable(String)`: Drex's response body as JSON text |

## The typed functions

`drex_noul`, `drex_choice` and `drex_score` ask one question about a text `input` (`Nullable(String)`, sent to Drex
as a JSON string).

- **`instructions`** is the question. `''` or `NULL` leaves it out; a `noul` then needs a described `true` or
  `false` in its criteria.
- **`criteria`** (`drex_noul`) is JSON text, or `''` for none, in one of three forms: a list of labels
  (`'["true", "false"]'`), a list of `{"label", "description"}` objects, or an object of label to description
  (`'{"true": "The customer wants money back", "false": null}'`). The labels are exactly `true` and `false`; labels
  with no description are the same as no criteria.
- **`labels`** (`drex_choice`) and **`levels`** (`drex_score`) are `Array(String)`: 2 to 255 unique, non-empty
  labels. Score levels are a scale in order; describe it in the instructions. For labels with descriptions, use
  `drex_decide`.
- **`model`** is a model name `GET /v1/models` lists, or `''` (or `NULL`) for `drex-v2.0`.

Results come from Drex's answer, never invented:

- A `noul` is the probability of yes. Its confidence, as Drex computes it, is `greatest(p, 1 - p)`.
- A `choice` has the chosen label, every label's probability sorted by label, and Drex's confidence.
- A `score` has the expected level index (0 for the first level), every level's probability in scale order with its
  index, and Drex's confidence.

A NULL `input` returns NULL without a request: a NULL `noul`, or a tuple with NULL `choice` or `score`, an empty
`probabilities`, and NULL `confidence` (a ClickHouse tuple can't itself be NULL). So does a row whose request fails
(see [Errors](#errors)).

## `drex_decide`

```sql
SELECT drex_decide(
    '{"message": "I was charged twice"}',
    '{"refund": {"type": "noul", "instructions": "Is a refund needed?"},
      "team": {"type": "choice", "instructions": "Which team?", "criteria": {"billing": "Payments", "technical": null}}}',
    ''
);
```

- **`state`** is JSON text: a string, an object, or an array. It must be valid JSON; NaN and Infinity are refused.
- **`questions`** is a JSON object of question id to `{"type", "instructions", "criteria"}`: 1 to 512 questions, with
  ids that are non-empty and unique ignoring case. `type` is `noul`, `choice` or `score` (`multiple_choice` and `box`
  are refused), and no other key is allowed. `criteria` takes the three forms above for `noul` and `choice`, and a
  list of levels for `score`; `choice` and `score` need criteria. The id `__proto__` is refused, since Drex drops its
  answer.
- **`model`**: as above, `drex-v2.0` by default.

The result is Drex's whole response body (`model`, `answers`, `usage`, `evaluation_time_ms`, `request_id`) as JSON
text; read it with `JSONExtract` or `::JSON`. A NULL `state` or `questions` returns NULL without a request.

## `drex_decide_image`

`drex_decide` with images. **`images`** is an `Array(String)` of 1 to 8 PNG, JPEG or WebP images as bytes, for
example from `file('photos/*.png', 'RawBLOB')` or an S3 table function. The function sends the bytes; Drex never
fetches a URL. The model is `drex-max` by default, and must be one that reads images (`drex-max`, `drex-max-v1.1`).
An empty array returns NULL without a request.

The request body, images included and base64-encoded, must stay within Drex's 4 MiB, so images of up to about 3 MiB
in total fit in one row.

## Constants

`instructions`, `criteria`, `labels`, `levels`, `questions` and `model` are meant to be constants, but ClickHouse
passes them per row. Each block's distinct values are checked once, before any request, and a value that fails a
check fails the query, whichever row it is on.

## Errors

| Outcome | When |
| --- | --- |
| **The query fails** (`UDF_EXECUTION_FAILED`, with the reason) | 401 (Drex's message); 402 (Drex's message and the billing page); a 422 about anything other than the row (each issue as `path: message`); an unknown model; images with a model that doesn't read them; a TLS failure; any check above; an argument without its null flag, which ClickHouse before 26.7 sends |
| **NULL, with a warning** | a 422 about the row (`Request too long:`, or issues only under `state` or `images`); a body over 4 MiB, never sent; an input that isn't valid UTF-8; an image that isn't a PNG, JPEG or WebP; retries exhausted; any other status (for example 403 or 404) |
| **Retried** | no response, 408, 429, 500, 502, 503, 504 or 529 |

Treat NULL as an unsuccessful answer, not a negative one. A warning never quotes the row: it is written to the
function's stderr, which ClickHouse logs as `Executable generates stderr at the end: drex_<function>: a row returned
NULL: <reason>`, in the server log and `system.text_log`.

A request is tried at most 5 times within 120 s of the first attempt, each attempt up to 60 s. Each retry waits for
Drex's `retry-after-ms` (or `retry-after`), at most 30 s, or else 1, 2, 4 and 8 s less up to a quarter. Every
attempt sends the same body.

## Configuration

Each function's `<command>` in `config/drex_function.xml` runs `drex_udf.py` with:

| Option | |
| --- | --- |
| `--function` | `noul`, `choice`, `score`, `decide` or `decide_image` |
| `--key-file` | A file holding the API key, readable only by the `clickhouse` user |
| `--concurrency` | Requests in flight per process (at most 4 on ClickHouse Cloud, whose processes may start only 5 threads) |
| `--base-url` | The API, `https://console.nace.ai` by default. HTTPS is required except to `localhost` |
| `--openrouter-referer` | Optional. The `HTTP-Referer` app identity on OpenRouter, `https://nace.ai` by default |
| `--openrouter-title` | Optional. The `X-OpenRouter-Title` app name on OpenRouter, `Drex for ClickHouse` by default |

For OpenRouter, set `--base-url https://openrouter.ai/api` and put an OpenRouter key (`sk-or-...`) in the key file. A
`nace_` key is refused before any request. Model names without an owner are sent under `nace-ai/`, and the models are
those OpenRouter lists. The two attribution headers are sent only to OpenRouter; pass `--openrouter-referer` and
`--openrouter-title` to report your own app instead of Nace's.

On ClickHouse Cloud, `cloud/build_bundles.py` reads `NACE_BASE_URL`, then takes the key from `OPENROUTER_API_KEY` for an
OpenRouter URL (else `NACE_API_KEY`), and writes `NACE_OPENROUTER_REFERER` and `NACE_OPENROUTER_TITLE`, when set, to
`config.json` as `openrouter_referer` and `openrouter_title`.

Every request carries `X-Nace-Client: drex-clickhouse/<version>`.

---

ClickHouse is a registered trademark of ClickHouse, Inc. [clickhouse.com](https://clickhouse.com/).
