FROM python:3.11-slim
WORKDIR /app
COPY DNS/ .
ENV DNS_ROLE=resolver
# root | tld | auth | resolver
CMD ["sh", "-c", "python3 ${DNS_ROLE}_server.py"]
