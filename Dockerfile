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
# The test module test_execute_tpcds lives in, at the labeling environment's versions:
# the selector runs that test (BR_USE_TPCDS_RUN).
RUN pip install --no-cache-dir --no-deps duckdb==2.0.0 numpy==2.5.5 pandas==3.1.1 pytz==2026.6 tzdata==2026.6 \
    && python -c "import duckdb, pandas.testing"

COPY selector.py base_selector.py more.py execrun.py warm.py tpcds_harness.py genmem_harness.py build_duck.py /app/
COPY map /app/map
# The disk is read-only at run time: compile everything now.
RUN python /app/build_duck.py /opt/sqlglot /app/map/tpcds_duck.pkl
RUN chmod -R a+rX /opt/sqlglot /app && python -m compileall -q /app /opt/sqlglot/sqlglot /opt/sqlglot/tests/helpers.py /opt/sqlglot/tests/test_executor.py
ENV PYTHONDONTWRITEBYTECODE=1
ENV BR_USE_TPCDS_RUN=1
# Imported as a module so the bytecode compiled above is used (a script is recompiled every run).
CMD ["python", "-c", "import sys; sys.path.insert(0, '/app'); import selector; selector.main()"]
