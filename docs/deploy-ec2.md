# Deploying to AWS EC2

How to run the whole system on one EC2 instance with Docker Compose, so that the review app opens at a public URL. Only the review app is public: its nginx forwards `/api` to the gateway inside Docker, and the database, the case service and the gateway stay on the instance's 127.0.0.1. To run the app on your own machine instead, see [local-testing.md](local-testing.md).

A first deployment takes one to two hours.

## What you need

- An AWS account that can launch EC2 instances.
- An OpenAI API key, and the LangFuse project's public key, secret key and host.
- The code to deploy merged into `main`: the instance clones `main`.

## 1. Launch an instance

In the AWS console, open EC2 and choose **Launch instance**:

| Setting | Value |
|---|---|
| Name | `affirm-agent` |
| AMI | Ubuntu Server 24.04 LTS, 64-bit (x86) |
| Instance type | `t3.small` (2 GiB). A 1 GiB `t3.micro` also works, but it needs swap (step 2) and builds slowly |
| Key pair | Create a new RSA key pair in `.pem` format; the browser downloads it |
| Network settings | Allow SSH traffic from **My IP**, and allow HTTP traffic from the internet. Add no other rules: 5432, 8001 and 8080 must never be open |
| Storage | 20 GiB, gp3 |

Then note the instance's **Public IPv4 address**. At rest, the six containers use about 0.5 GB of memory; building the images on the instance takes more.

## 2. Connect and install Docker

On your own machine, with the key file you downloaded:

```bash
chmod 400 ~/Downloads/affirm-agent.pem
ssh -i ~/Downloads/affirm-agent.pem ubuntu@<public IP>
```

On the instance, install Docker Engine with its Compose and Buildx plugins from Docker's apt repository:

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl git
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker ubuntu
exit
```

Connect again, since the `docker` group applies from the next login, and check that `docker compose version` prints a version.

With 1 GiB of memory, add 2 GiB of swap before building:

```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

## 3. Get the code and fill in `.env`

```bash
git clone https://github.com/kimhw99/affirm-agent.git
cd affirm-agent
cp .env.example .env
for v in POSTGRES_PASSWORD AUTH_DB_PASSWORD CASE_DB_PASSWORD AGENT_DB_PASSWORD CASE_SERVICE_TOKEN JWT_SECRET; do
  sed -i "s/^$v=.*/$v=$(openssl rand -hex 32)/" .env
done
nano .env
```

The loop gives each of the six secrets its own random value, without printing it. In `nano`, set the rest, then save with Ctrl+O and Enter, and quit with Ctrl+X:

| Variable | Set it to |
|---|---|
| `APP_BIND_ADDRESS` | `0.0.0.0`, so that the review app listens beyond the instance |
| `APP_HOST_PORT` | `80` |
| `OPENAI_API_KEY` | Your OpenAI key |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST` | The LangFuse project's keys and host |
| `DEMO_ANALYST_PASSWORD`, `DEMO_SENIOR_PASSWORD` | Strong passwords: anyone who has the URL can try them |

Fill in `.env` before the first start: the database sets its passwords up only when its volume is created ([local-testing.md, Troubleshooting](local-testing.md#troubleshooting)). This prints nothing once every placeholder is replaced:

```bash
grep -n "change-me\|XXX" .env
```

`.env` stays on the instance. Never commit it or paste it anywhere.

## 4. Start the app and load the data

```bash
docker compose up -d --build --wait            # the first build takes several minutes
docker compose run --rm case python seed.py    # loads the 119 golden cases into case_db
docker compose ps                              # all six services healthy
```

## 5. Check it

- Open `http://<public IP>/`, log in as `analyst`, and run a review of case_1042. With an OpenAI key, the memo arrives in about half a minute to a minute, with its evidence.
- Log in as `senior` and open the override rate, which only a senior analyst sees.
- In LangFuse, find that review's trace.
- From your own machine, `curl -m 5 http://<public IP>:8080/health` must fail: the gateway isn't public.

## Running it

From `affirm-agent` on the instance:

| To | Run |
|---|---|
| Deploy the latest `main` | `git pull`, then `docker compose up -d --build --wait` |
| Start over with fresh data, for example before a demo | `docker compose run --rm case python seed.py --reset`, then `docker compose restart agent` (the agent keeps reviews in memory) |
| Read the logs | `docker compose logs --tail 50 gateway agent case` |
| Stop the app and keep the data | `docker compose down` |

The containers don't start on their own when the instance restarts: run `docker compose up -d --wait` again. An instance that was stopped and started also gets a new public IP address, so the URL changes.

When the instance is no longer needed, stop or terminate it in the EC2 console (terminating deletes its disk), and revoke any key that was shared.

## Troubleshooting

| Problem | Check |
|---|---|
| The page doesn't open | The security group allows HTTP; `.env` has `APP_BIND_ADDRESS=0.0.0.0` and `APP_HOST_PORT=80`; `docker compose ps` shows `frontend` healthy |
| The build stops, or the instance stops answering | Not enough memory: add swap (step 2), or use `t3.small` |
| `docker` says permission denied | Connect again after `usermod` (step 2) |
| Every memo recommends Insufficient evidence | `OPENAI_API_KEY` in `.env`; `docker compose logs agent` shows the error |
| No traces in LangFuse | The two LangFuse keys and `LANGFUSE_HOST` in `.env` |
| Login fails | The demo passwords in `.env`; after changing them, run `docker compose up -d` |

## If EC2 can't run it

When the app can't run reliably on the instance, the project requirements allow a complete local demonstration instead ([local-testing.md](local-testing.md)). It must still show Dockerized services, full-stack integration, microservice communication, agent workflows, access control, LangFuse traces, guardrails and the golden dataset evaluation.
