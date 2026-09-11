# Auth Broker: the only workload permitted to call KMS asymmetricSign.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY cloud/auth_broker/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# The broker needs the package and the policy files it enforces. It does not
# need, and is not given, any key material: the private key lives in KMS.
COPY src/ /app/src/
COPY config/ /app/config/

ENV PYTHONPATH=/app/src DELEGATION_SIGNER=kms
EXPOSE 8080

# Cloud Run supplies $PORT; bind 0.0.0.0 so the platform can reach it.
CMD exec uvicorn governance_demo.auth_broker_app:app --host 0.0.0.0 --port ${PORT:-8080}
