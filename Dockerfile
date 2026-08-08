FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1

# Install dependencies first (cache layer)
COPY bot_requirements.txt .
RUN pip install --no-cache-dir -r bot_requirements.txt

# Copy the worker and its shared validation/transport layers.
COPY discord_bot.py dca_config.py github_contents.py ./

CMD ["python3", "-u", "discord_bot.py"]
