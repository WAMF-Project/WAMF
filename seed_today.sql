INSERT INTO detections (
    detection_time,
    detection_index,
    score,
    display_name,
    category_name,
    frigate_event,
    camera_name
)
VALUES

(datetime('now', '-55 minutes'), 1, 0.94, 'Cyanistes caeruleus', 'bird', 'dev_today_bt_001', 'birdcam'),

(datetime('now', '-52 minutes'), 1, 0.91, 'Passer domesticus', 'bird', 'dev_today_hs_001', 'birdcam'),

(datetime('now', '-48 minutes'), 1, 0.88, 'Dendrocopos major', 'bird', 'dev_today_gsw_001', 'birdcam'),

(datetime('now', '-43 minutes'), 1, 0.96, 'Turdus migratorius', 'bird', 'dev_today_ar_001', 'birdcam'),

(datetime('now', '-39 minutes'), 1, 0.89, 'Cyanocitta cristata', 'bird', 'dev_today_bj_001', 'birdcam'),

(datetime('now', '-35 minutes'), 1, 0.87, 'Parus major', 'bird', 'dev_today_gt_001', 'birdcam'),

(datetime('now', '-31 minutes'), 1, 0.92, 'Sturnus vulgaris', 'bird', 'dev_today_es_001', 'birdcam'),

(datetime('now', '-27 minutes'), 1, 0.90, 'Garrulus glandarius', 'bird', 'dev_today_ej_001', 'birdcam'),

(datetime('now', '-22 minutes'), 1, 0.95, 'Cyanistes caeruleus', 'bird', 'dev_today_bt_002', 'birdcam'),

(datetime('now', '-18 minutes'), 1, 0.93, 'Passer domesticus', 'bird', 'dev_today_hs_002', 'birdcam'),

(datetime('now', '-14 minutes'), 1, 0.91, 'Turdus migratorius', 'bird', 'dev_today_ar_002', 'birdcam'),

(datetime('now', '-9 minutes'), 1, 0.88, 'Sturnus vulgaris', 'bird', 'dev_today_es_002', 'birdcam'),

(datetime('now', '-4 minutes'), 1, 0.97, 'Cyanocitta cristata', 'bird', 'dev_today_bj_002', 'birdcam');