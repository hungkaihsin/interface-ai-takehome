# One image containing both the automation and the legacy app it drives.
#
# One container rather than two, deliberately. The artifacts bind to a concrete
# entry URL and carry an origin allowlist -- that is the multi-tenant design, not an
# oversight -- so splitting the target app into its own service would mean rewriting
# every stored artifact to point at a container hostname. Keeping both on localhost
# means the committed artifacts run unchanged, which is the point of a deterministic
# replay you can hand to someone else.

FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first so edits to source do not invalidate the browser layer, which
# is by far the slowest to rebuild.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
 && python -m playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/*

COPY . .

# The target app. Published so you can open the legacy console in your own browser
# and see what the automation is driving.
EXPOSE 5001

ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["demo"]
