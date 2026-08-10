# Tool Gateway

The Tool Gateway admits or refuses every tool call an agent proposes. It is
owned by team Northwind, and it returns the `AWB-51xx` family of refusal codes.

Gateway decisions are written to the `aw-core` cluster as part of the tool
execution ledger, so a refusal survives the process that made it.
