# Documentation

| Document | Read it to |
| --- | --- |
| [architecture.md](architecture.md) | see how the code is layered, which boundaries are enforced, and what each module is |
| [register-facts-protocol.md](register-facts-protocol.md) | follow the review flow for saving facts from a chat |
| [competency-questions.md](competency-questions.md) | see the questions the database must answer, each tied to a test (draft) |
| [cli.md](cli.md) | look up every `knowledge.py` command and option (generated from the code) |
| [mcp.md](mcp.md) | register the MCP server, choose its mode, and see what it decides for Claude |
| [skills.md](skills.md) | install the Claude skills that use the knowledge base |
| [db-source-of-truth.md](db-source-of-truth.md) | follow the design for making the database the source of truth, what is built (export, snapshots, restore, log tables) and what is not |
| [prior-art.md](prior-art.md) | see what existing tools were compared for the database design and for Claude access |
| [build-contracts.md](build-contracts.md), [build-acceptance.md](build-acceptance.md) | read the contracts the build keeps and the checks that accept a build |
| [releasing.md](releasing.md) | write a changelog fragment and cut a release with towncrier and bump-my-version |
| [onion-migration-handoff.md](onion-migration-handoff.md) | read the history of the move to the onion layout (historical) |

For installation and day-to-day commands, start with the [project README](../README.md).
