import sqlite3

import numpy as np
from PIL import Image

from tflite_support.task import core
from tflite_support.task import processor
from tflite_support.task import vision


MODEL_PATH = "model.tflite"
IMAGE_PATH = "test.jpg"
DB_PATH = "birdnames.db"


# ---------------------------------------------------
# INITIALIZE CLASSIFIER
# ---------------------------------------------------

base_options = core.BaseOptions(
    file_name=MODEL_PATH,
    use_coral=False,
    num_threads=4
)

classification_options = processor.ClassificationOptions(
    max_results=10,
    score_threshold=0
)

options = vision.ImageClassifierOptions(
    base_options=base_options,
    classification_options=classification_options
)

classifier = vision.ImageClassifier.create_from_options(
    options
)

print("Classifier loaded!")
print()


# ---------------------------------------------------
# LOAD IMAGE
# ---------------------------------------------------

image = Image.open(IMAGE_PATH).convert("RGB")

image = image.resize((224, 224))

image_np = np.array(
    image,
    dtype=np.uint8
)

image_np = np.ascontiguousarray(image_np)

tensor_image = vision.TensorImage.create_from_array(
    image_np
)

# ---------------------------------------------------
# RUN INFERENCE
# ---------------------------------------------------

result = classifier.classify(
    tensor_image
)

categories = result.classifications[0].categories


# ---------------------------------------------------
# DATABASE LOOKUP
# ---------------------------------------------------

conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()

print("TOP PREDICTIONS\n")

for category in categories:

    scientific_name = category.display_name
    score = category.score

    cursor.execute(
        """
        SELECT common_name
        FROM birdnames
        WHERE scientific_name = ?
        """,
        (scientific_name,)
    )

    row = cursor.fetchone()

    if row:
        common_name = row[0]
    else:
        common_name = "Unknown"

    print(
        f"{common_name:30} "
        f"{score:.3f} "
        f"({scientific_name})"
    )

conn.close()