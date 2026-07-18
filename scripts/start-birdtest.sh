ffmpeg \
-re \
-stream_loop -1 \
-i /home/ian/frigate-test-videos/birdtest2.mp4 \
-an \
-c:v copy \
-rtsp_transport tcp \
-f rtsp \
rtsp://192.168.1.98:8555/birdtest