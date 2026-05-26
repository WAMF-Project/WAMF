import sqlite3

import numpy as np
import tensorflow as tf

from PIL import Image


MODEL_PATH = "model.tflite"
IMAGE_PATH = "test.jpg"
DB_PATH = "birdnames.db"


# ---------------------------------------------------
# LOAD MODEL
# ---------------------------------------------------

print("Loading model...")

interpreter = tf.lite.Interpreter(
    model_path=MODEL_PATH
)

interpreter.allocate_tensors()

input_details = interpreter.get_input_details()
output_details = interpreter.get_output_details()

print("Model loaded!")

# ---------------------------------------------------
# LOAD IMAGE
# ---------------------------------------------------

image = Image.open(IMAGE_PATH).convert("RGB")

# Match original WAMF behavior
image = image.resize((224, 224))

# Convert directly to uint8
input_data = np.array(
    image,
    dtype=np.uint8
)

# Add batch dimension
input_data = np.expand_dims(
    input_data,
    axis=0
)

# ---------------------------------------------------
# RUN INFERENCE
# ---------------------------------------------------

print("Running inference...")

interpreter.set_tensor(
    input_details[0]['index'],
    input_data
)

interpreter.invoke()

output_data = interpreter.get_tensor(
    output_details[0]['index']
)

scores = output_data[0]

# ---------------------------------------------------
# OUTPUT QUANTIZATION
# ---------------------------------------------------

output_scale, output_zero_point = (
    output_details[0]['quantization']
)

# ---------------------------------------------------
# GET TOP 10 RESULTS
# ---------------------------------------------------

top_indices = np.argsort(scores)[-10:][::-1]

# ---------------------------------------------------
# OPEN DATABASE
# ---------------------------------------------------

conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()

print("\nTOP 10 PREDICTIONS\n")

for idx in top_indices:

    raw_score = scores[idx]

    confidence = (
        raw_score - output_zero_point
    ) * output_scale

    # ---------------------------------------------------
    # EXPERIMENTAL LABEL LOOKUP
    # ---------------------------------------------------

    # We ASSUME model index maps to SQLite rowid
    # but this may be incorrect.
    rowid = int(idx) + 1

    cursor.execute(
        """
        SELECT scientific_name, common_name
        FROM birdnames
        WHERE rowid = ?
        """,
        (rowid,)
    )

    result = cursor.fetchone()

    if result:
        scientific_name, common_name = result
    else:
        scientific_name = "Unknown"
        common_name = "Unknown"

    print(
        f"Index: {idx:<4} "
        f"Confidence: {confidence:.3f}   "
        f"{common_name} "
        f"({scientific_name})"
    )

conn.close()