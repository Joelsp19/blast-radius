# Blast Radius entry. Same base image (digest) the commits were labeled on.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir -r /app/requirements.lock

# sqlglot at the pinned commit, straight from its GitHub repository.
ARG SQLGLOT_SHA=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
RUN python -c "import io,tarfile,urllib.request,os; \
d=urllib.request.urlopen('https://github.com/tobymao/sqlglot/archive/${SQLGLOT_SHA}.tar.gz').read(); \
t=tarfile.open(fileobj=io.BytesIO(d)); t.extractall('/opt', filter='data'); \
os.rename('/opt/sqlglot-${SQLGLOT_SHA}', '/opt/sqlglot')" \
 && python -m compileall -q /opt/sqlglot/sqlglot /opt/sqlglot/tests \
 && chmod -R a+rX /opt/sqlglot

COPY br /app/br
RUN python /app/br/precompile.py && PYTHONPATH=/opt/sqlglot SKIP_INTEGRATION=0 PYTHONDONTWRITEBYTECODE=1 PYTHONHASHSEED=0 python /app/br/precompile.py state && PYTHONPATH=/opt/sqlglot SKIP_INTEGRATION=0 PYTHONDONTWRITEBYTECODE=1 PYTHONHASHSEED=0 python /app/br/precompile.py setup && python -m compileall -q /app/br && chmod -R a+rX /app

ENV PYTHONPATH=/opt/sqlglot SKIP_INTEGRATION=0 PYTHONDONTWRITEBYTECODE=1 PYTHONHASHSEED=0 OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1
CMD ["python", "/app/br/selector.py"]
