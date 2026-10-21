# Checks to run in a real Claude chat

`nebula eval` covers extraction and retrieval without Claude. These cover the part only a real
conversation shows: whether Claude picks the right Nebula tool, reads enough, and tells the person
what's happening. Run each in a fresh chat with the Nebula connector on, note the tool calls Claude
made and roughly how much it read, and mark it clear, confusing or wrong.

| Say this | Good looks like |
|---|---|
| "Do we have [a short document's name]?" | `search` first, then answers from the whole file (it comes back whole), cites it. |
| "Summarise the [long doc]" | `search`, then `open` page by page; doesn't summarise from search excerpts. |
| "I've just updated [doc link]. Use the new version." | `sources sync` with the link; says roughly how long; waits for the job (or says it will be ready shortly) before answering from it. |
| "Save this file to Nebula: [link to a file shared with Nebula]" | `sources connect`/`sync`; reports the job and wait. |
| "Save this: [link to a file NOT shared with Nebula]" | Says to share it with the service account address first; doesn't pretend it's saved. |
| "Is [scanned PDF] indexed properly?" | `sources status` or `open`; reports the parts not read; offers to read them. |
| "Read the chart on page 2 of [PDF] and keep it" | `open` with part + as_image; transcribes faithfully; `save` kind=transcription with expected_version. |
| "What's in [folder shared with me]?" | Lists only files Drive shares with the person. |
| A question Nebula has no answer to | Says Nebula doesn't have it; doesn't guess. |

For each, also note: number of tool calls, any repeated searches that added nothing, and whether a
result was cut ("Nebula cut this result…").
