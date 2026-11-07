#!/bin/sh
# বুটের সময় yt-dlp ও gallery-dl সর্বশেষ ভার্সনে আপডেট (বন্ধ করতে UPDATE_ON_BOOT=0)
if [ "${UPDATE_ON_BOOT:-1}" = "1" ]; then
  pip install -U --user --quiet yt-dlp gallery-dl || echo "আপডেট এড়ানো হলো"
fi
exec gunicorn -b 0.0.0.0:${PORT:-7860} -w 1 --threads 8 --timeout 600 app:app
