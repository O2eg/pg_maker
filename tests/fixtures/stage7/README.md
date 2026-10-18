# Documentation fixtures

Directories 17.5 and 18.1 contain the exact upstream guc-log-min-messages varlistentry
from their config.sgml, surrounded by a small DocBook 4.5 book/chapter/reference/table
and nested parameter entities. upstream.json records provenance and upstream file
SHA256; COPYRIGHT retains the license. Golden SGML specifies exact title replacement
without changing code, comments, entity references or unrelated bytes.

Unit tests use a minimal DTD. Docker tests use Debian's real DocBook 4.5 catalog,
xsltproc, FOP and Sphinx. Their small Makefile is an original fixture for planning,
isolation and packaging, not a PostgreSQL Makefile copy. Full manuals are separately
checked with native PostgreSQL targets.

The small Russian translation is explicitly supplied by the integration test through
a TranslationBundle. It checks extraction/application, structure preservation and
rendering; it does not imply that a complete Russian PostgreSQL translation is bundled.
