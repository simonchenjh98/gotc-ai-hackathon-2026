# PR #11: upstream protocol failures and file isolation

The review of `19cded78` reproduced two remaining gateway defects. This follow-up keeps the existing team-key login protection and the student's gateway implementation.

## Upstream protocol failures

Catch `BadStatusLine` and `LineTooLong` while opening the upstream response, using the same failure path as transport errors. Once a request has been sent, an invalid status line or oversized response header cannot prove that the upstream did no work: return a provider/network 502, record the reserved estimate once, then release the reservation and concurrency slot. Do not retry these requests. Do not catch all `HTTPException` types here: an invalid configured URL can fail before sending and must retain the existing free error handling. Connection and TLS failures before sending remain free; `count_tokens` always remains free.

## Shared upstream files

The gateway has no per-team ownership records for files stored in the shared upstream account. Reject those file references before forwarding instead of introducing an unverified ownership scheme. Cover OpenAI Responses file/image inputs and computer screenshots, Chat Completions file inputs, and Anthropic file sources and container uploads, including nested tool results and `count_tokens`.

Inspect protocol content structures only. Ordinary text, function arguments, tool schemas, metadata, inline media and URL media must remain usable. The gateway does not interpret a string mentioning `file_id` as a file reference.

## Verification

Add real HTTP regressions that fail on the reviewed version and pass after the fix. Check response status, attribution, single usage record, budget consumption, reservation cleanup and no retry for protocol errors; check no upstream request and zero charge for rejected files, with positive compatibility cases. Run the backend suite, frontend regressions and repository checks, then push a normal follow-up commit to the existing PR branch and check GitHub CI.
