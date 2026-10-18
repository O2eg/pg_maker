# Component manuals

These editable DocBook guides accompany the component recipes in `../components`.
They are included only when their component is selected and documentation is enabled.
The writer adds them to the upstream `contrib` appendix, using the same HTML theme,
navigation, full-text search, PDF and translation pipeline as the rest of the book.

| Guide | Source release | Pinned commit |
| --- | --- | --- |
| pg-stat-kcache.sgml | pg_stat_kcache 2.3.2 | 1e39814453ab747a3817cfdf4f6f6bd5c0fb9507 |
| pg-wait-sampling.sgml | pg_wait_sampling 1.1.11 (SQL version 1.1) | 7e3a5fc937393596c0a51c5881dd63d7208408d1 |

Guides contain distribution-specific setup instructions and references to those
exact upstream revisions. Review the matching guide when changing a recipe's pin;
resolve fixes both source commits and documentation asset hashes in the lock.
These are maintained guides, not automatic imports of arbitrary upstream README files.
The pg_stat_statements manual already belongs to the selected PostgreSQL source tree.

English is the source language. Russian builds require reviewed translation bundles
for the new sections under the existing translation policy.
