# Stage 2: Authorized capture experimental extension

This extension is **disabled by default**. The existing `web` and `worker` keep working without this module.

## Purpose

A dedicated `capture` dyno records a direct, authorized media stream with FFmpeg into approximately two-second transport-stream segments on its own filesystem. Chat spikes are stored in the existing Postgres `highlights` table. `capture` polls that table and, once sufficient post-event footage exists, renders approximately 60 seconds (20s before, 40s after), uploads the MP4 to S3-compatible object storage and records its location in the new `clips` table.

The **current implementation is experimental**, not the completed Shorts pipeline: no audio event detection, no transcript or subtitles, no smart crop, no top-ten final ranking, no web playback. HLS source and event timestamps may be offset due to network latency. Calibrate `CAPTURE_CLOCK_OFFSET_SECONDS` in a test stream. Segment discontinuities or delayed keyframes may produce incomplete clips. Do not describe stored clips as guaranteed 60-second output without checking actual media duration.

## Permission and delivery requirements

- Capture only an authorized source, supplied directly by the broadcaster. A public Twitch channel page or ordinary Twitch OAuth chat token does **not** grant a media capture URL or rights.
- Do not set `CAPTURE_PERMISSION_CONFIRMED=true` unless the content owner has approved recording and editing.
- Use a separate persistent object store (S3 / Cloudflare R2). Heroku files disappear upon dyno restart; web, chat worker and capture dynos do **not** share a filesystem.
- Require the FFmpeg binary: add Heroku Apt buildpack **before** Python buildpack for `Aptfile`, or deploy a suitable container. Confirm your build actually installs FFmpeg before starting the capture dyno.
- Plan for a dedicated always-on worker tier. An Eco dyno may sleep or exhaust shared monthly hours and is not suitable for guaranteed continuous recording.
- Set capture-specific variables only for the capture dyno if your process configuration supports that isolation; otherwise treat them as app-wide secrets and scope appropriately.

## Config Vars required to turn on capture

```
CAPTURE_PERMISSION_CONFIRMED=true
CAPTURE_SOURCE_URL=https://authorized-owner.example/live.m3u8
CAPTURE_CHANNEL=approved_channel
S3_BUCKET=private-gta-shorts-clips
S3_ENDPOINT_URL=https://ACCOUNTID.r2.cloudflarestorage.com # if using R2
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
CAPTURE_CLOCK_OFFSET_SECONDS=0
```

The source URL above is only a placeholder; the capture process is **not ready to launch** until you have a genuine permitted source and storage credentials. It must match the chat-monitored channel. Never commit these values to GitHub or share them in screenshots.

## Deploy safely

1. Commit the changes and deploy. Keep `capture=0` in Heroku Resources initially. Existing `web=1` and `worker=1` should continue to operate.
2. Install and verify FFmpeg. Set persistent storage credentials, permission flag and direct source URL as above.
3. Verify the media feed is an authorized, stable URL and source stream quality is appropriate; check permissions for eventual public reposting.
4. Turn on `capture=1` only when all prerequisites pass. It creates the `clips` table on startup. Monitor logs and storage usage.
5. Inspect the clips in the object storage console. Their URIs are saved as private `s3://` references; downloading via the website is not implemented yet.

## Local tests

```
python -m unittest discover -s tests -v
```

Run FFmpeg integration tests against a consented test stream. Mock-based tests do not validate live HLS recording, timeline sync or Heroku capacity.
