# Who may read a chunk

PostgreSQL is the authority on document permissions. The vector index stores a
copy of the ACL so a query returns fewer candidates, but that copy records what
was true when the document was last indexed. Every candidate is re-checked
against PostgreSQL before it becomes context, and the answer is withheld if a
source stopped being readable while the model was writing.
