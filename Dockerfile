FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

RUN useradd --uid 1000 --create-home pantry \
    && mkdir -p /data/oauth \
    && chown -R pantry:pantry /data
USER pantry

# Default meal plan range starts "today": use the same local date as Romain
ENV TZ=Europe/Paris \
    FASTMCP_HOME=/data/oauth

EXPOSE 8000
CMD ["pantry-mcp"]
