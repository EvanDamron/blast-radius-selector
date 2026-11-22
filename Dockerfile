# Same base image the labels were made in (linux/amd64 on the scorer).
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# The pinned sqlglot source: the selector diffs each patch against it.
ARG SQLGLOT_COMMIT=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
COPY fetch_sqlglot.py /tmp/fetch_sqlglot.py
RUN python /tmp/fetch_sqlglot.py ${SQLGLOT_COMMIT} /opt/sqlglot && test -f /opt/sqlglot/sqlglot/parser.py

# sqlglot imports these at run time when present (typing_extensions once an
# `if TYPE_CHECKING:` block runs), and the labels were made with them
# (starter/requirements.lock): without dateutil, date arithmetic in the optimizer is
# skipped, so proofs run without it could miss a change there.
RUN pip install --no-cache-dir --no-deps python-dateutil==4.0.0 mypy_extensions==1.2.0 typing_extensions==4.16.0 \
    && python -c "from dateutil.relativedelta import relativedelta; import mypy_extensions, typing_extensions"

COPY selector.py base_selector.py more.py execrun.py warm.py tpcds_harness.py genmem_harness.py /app/
COPY map /app/map
# The disk is read-only at run time: compile everything now.
RUN chmod -R a+rX /opt/sqlglot /app && python -m compileall -q /app /opt/sqlglot/sqlglot /opt/sqlglot/tests/__init__.py /opt/sqlglot/tests/helpers.py /opt/sqlglot/tests/test_optimizer.py
ENV PYTHONDONTWRITEBYTECODE=1
# Prove fixture-driven optimizer tests unaffected by re-running the entries a patch reaches.
ENV BR_USE_OPTFIX=1
# Imported as a module so the bytecode compiled above is used (a script is recompiled every run).
CMD ["python", "-c", "import sys; sys.path.insert(0, '/app'); import selector; selector.main()"]
