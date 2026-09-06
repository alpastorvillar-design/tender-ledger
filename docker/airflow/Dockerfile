# Tender Ledger inside the official Airflow runtime.
#
# Build from the repository root:
#   docker compose -f compose.yaml -f compose.airflow.yaml build
#
# The base image is pinned by its manifest digest, and the install below is
# constrained to the same Airflow version, so adding this project can never
# move the Airflow the scheduler runs. The build fails if either happens.
FROM apache/airflow:3.3.1-python3.13@sha256:0c4bcc0370e526de1b7892a3bf4343d260c6c82359c66f77155b53cd773d6339

# Only what the package needs to build and install: no data, no .env, no tests.
COPY --chown=airflow:root docker/airflow/constraints.txt /tmp/build/constraints.txt
COPY --chown=airflow:root pyproject.toml /tmp/build/pyproject.toml
COPY --chown=airflow:root LICENSE /tmp/build/LICENSE
COPY --chown=airflow:root src /tmp/build/src

RUN pip install --no-cache-dir --constraint /tmp/build/constraints.txt /tmp/build \
    && python -c "import airflow; assert airflow.__version__ == '3.3.1', airflow.__version__" \
    && python -m tender_ledger --help > /dev/null \
    && rm -rf /tmp/build
