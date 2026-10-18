# GUC insertion fixtures

Self-contained parser inputs written for pg_maker, with invented parameter names
and values. They are not excerpts from a database source tree and do not claim
compatibility with any particular PostgreSQL release or C structure definition.

The compact layout contains three tables; the expanded layout contains six,
including an int64 table after int and unrelated repeated bottom markers.
Both preserve the regression condition: choose the first bottom after each top,
leave other tables untouched, and reject a repeated insertion without a receipt.
Golden files independently specify insertions into the bool and int tables only.
