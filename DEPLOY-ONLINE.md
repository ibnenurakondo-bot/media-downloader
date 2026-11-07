# অনলাইনে ডেপ্লয় (ঐচ্ছিক, ফ্রি হোস্টে সীমাবদ্ধতা আছে)

ফ্রি ক্লাউড সার্ভারের IP-তে YouTube/Facebook/Instagram প্রায়ই ব্লক বা "Sign in" চায়, তাই এখানে ফল অনিশ্চিত।
নিজের ডিভাইসে চালানোই (README.md) বেশি নির্ভরযোগ্য।

## Hugging Face Spaces (Docker)
1. New Space → SDK: Docker → Blank।
2. সব ফাইল আপলোড করুন, এবং `README.md`-এর একদম শুরুতে নিচের ব্লক বসান:

       ---
       title: Media Downloader
       sdk: docker
       app_port: 7860
       ---

3. Settings → Secrets-এ প্রয়োজনে `ACCESS_CODE`, `COOKIES_B64`, `ADMIN_TOKEN` দিন।

## Render
রিপো GitHub-এ পুশ করে New → Blueprint (`render.yaml` আছে)।

## সার্ভার-মোডের ভেরিয়েবল
`ACCESS_CODE`, `COOKIES_B64` (cookies.txt-এর base64), `ADMIN_TOKEN`, `MAX_MINUTES` (30), `MAX_IMAGES` (30),
`RATE_LIMIT` (10/ঘণ্টা), `UPDATE_HOURS` (24)। কুকি দিলে অবশ্যই `ACCESS_CODE` দিন।
আপডেট: `curl -X POST https://<অ্যাপ>/api/admin/update -H "X-Admin-Token: <টোকেন>"`, ভার্সন: `/health`।
