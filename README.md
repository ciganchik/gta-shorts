# GTA Shorts Studio — Stage 1 (Twitch chat spike detection)

This is **not yet** the complete auto-clipping product. It checks a Twitch stream, connects to EventSub chat, records chat activity in Heroku Postgres and detects candidates using 10-second message counts. It does **not** capture stream video, analyze streamer audio, generate captions or render shorts.

## Requirements
- Twitch Developer application, registered with `https://YOUR-APP.herokuapp.com/auth/callback` as OAuth redirect URL.
- Twitch user authorization with `user:read:chat`. Some broadcasters require their own authorization or bot permissions to monitor their chat. Follow Twitch's policy and ask the broadcaster before using their content.
- Heroku account, Heroku CLI, a new Heroku app, Heroku Postgres add-on.
- Python 3.12 for local use.

## Deploy to Heroku

```bash
heroku login
heroku create YOUR-UNIQUE-APP-NAME
heroku addons:create heroku-postgresql:essential-0 -a YOUR-UNIQUE-APP-NAME
heroku config:set -a YOUR-UNIQUE-APP-NAME TWITCH_CLIENT_ID="..." TWITCH_CLIENT_SECRET="..." \
  TWITCH_REDIRECT_URI="https://YOUR-UNIQUE-APP-NAME.herokuapp.com/auth/callback" \
  ADMIN_KEY="GENERATE_A_LONG_RANDOM_SECRET" FERNET_KEY="GENERATE_FERNET_KEY"
git init
git add .
git commit -m "Initial Twitch monitor MVP"
heroku git:remote -a YOUR-UNIQUE-APP-NAME
git push heroku main
heroku ps:scale web=1 worker=1 -a YOUR-UNIQUE-APP-NAME
```

If your local branch is `master`, use `git push heroku master:main` instead. Postgres plan names, availability and pricing may change: verify in Heroku Dashboard before provisioning.

Generate secrets locally:
```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Visit `https://YOUR-UNIQUE-APP-NAME.herokuapp.com/login` and enter your `ADMIN_KEY` through the password form. Do not share the key. The browser uses an HTTP-only secure cookie for 8 hours. This is still a single-admin MVP, not a full user-account system. Restart the worker after authorizing Twitch:

```bash
heroku ps:restart worker -a YOUR-UNIQUE-APP-NAME
heroku logs --tail -a YOUR-UNIQUE-APP-NAME
```

Enter Twitch URL in the interface. If the channel requires broadcaster authorization, Twitch may reject the EventSub subscription. This is a permission issue, not a reason to bypass API access.

### Current limitations
- One monitored channel at a time. Worker requires restart after changing the channel in this initial build.
- No upload, recording, audio analysis, text-to-speech or MP4 export yet.
- The candidate detector has test parameters (>=8 messages/10s, >=3x local baseline, >=3 unique users, 75s cooldown). Tune using labeled test data before production.
- Raw messages are retained in Postgres indefinitely in this initial prototype; implement retention and privacy controls before broad deployment.
- Do not point it at unauthorized channels or redistribute Twitch content without permission.
- Heroku filesystem is ephemeral. Later video recording must use persistent object storage and a suitable recording/rendering worker, often outside Heroku.
- Single process app is intended for testing. Use migrations, monitoring and robust error handling for production.

## Tests
```bash
python -m unittest discover -s tests
```


## Mobile Safari deployment (without a laptop)

1. In iPhone Files, unzip the provided source archive. Create a private GitHub repository (`gta-shorts-studio`).
2. Use GitHub's web interface (request desktop website in Safari if needed), choose **Add file → Upload files** and upload the **contents of the unzipped project**, preserving `app/`, `tests/`, `Procfile`, `requirements.txt`, `.python-version` and `README.md`. The ZIP itself cannot be deployed as source by the GitHub integration.
3. In Heroku Dashboard, create an app with a globally unique name, open **Deploy → GitHub**, connect your repo and select **Deploy Branch**.
4. Under **Resources**, provision Heroku Postgres Essential-0 if available. Under **Settings → Reveal Config Vars** add `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`, `TWITCH_REDIRECT_URI`, `ADMIN_KEY`, `FERNET_KEY`. (`DATABASE_URL` is created by the Postgres add-on.)
5. Register the exact `https://APP-NAME.herokuapp.com/auth/callback` URL in your Twitch Developer application.
6. In Heroku **Resources**, scale `web=1`, `worker=1` using always-on dynos (Basic or suitable paid option). Worker and web each have separate costs. Check your current invoice before enabling.
7. Open `https://APP-NAME.herokuapp.com/login` in Safari, enter `ADMIN_KEY`, connect Twitch, then submit the channel link. If it changes, restart the worker via Heroku Dashboard.
8. Safari Share → Add to Home Screen for an app-like icon. This is a responsive website, not a native iOS application.

Important: the MVP is not a video downloader or a finished Shorts producer. Twitch OAuth permissions are required, and some chats cannot be monitored with a generic user token. Heroku dyno storage is temporary, so MP4 exports in a future version will require object storage or an external rendering machine.
