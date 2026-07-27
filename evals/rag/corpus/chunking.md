# Where a chunk begins

Documents are cut into overlapping windows measured in tokens. Which tokenizer
counts them decides where every boundary falls, so the counter's name is part
of the index identity. Overlap exists so a sentence spanning a boundary is
retrievable from either side.
