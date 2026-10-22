`bird/aiy-v1` is WAMF's immutable packaging revision of Google's AIY Birds V1
CPU classifier. It is an additional downloadable artifact; `bird/bundled-1`
remains unchanged and default. Installing it does not select or activate it.

Provenance and redistribution:

- [Coral's official model table](https://coral.ai/models/image-classification/)
  links the CPU model separately from the Edge TPU-compiled version.
- The exact [Coral CPU source](https://github.com/google-coral/test_data/blob/104342d2d3480b3e66203073dac24f4e2dbb4c41/mobilenet_v2_1.0_224_inat_bird_quant.tflite)
  is pinned to commit `104342d2d3480b3e66203073dac24f4e2dbb4c41`.
  Filename: `mobilenet_v2_1.0_224_inat_bird_quant.tflite`.
  Size: 3,579,500 bytes. SHA-256:
  `350fcd8cf1df1560060d464595dfed8b174b05792788052896004848d9ad04f9`.
- Google's [model-specific documentation](https://github.com/tensorflow/tfhub.dev/blob/81fdea8ffa53b5acbd66a012d17fb8f9602c714b/assets/docs/google/models/aiy/vision/classifier/birds_V1/1.md)
  explicitly licenses the model under Apache 2.0, identifies Google AIY Vision
  Kit Natural Explorer as the original publication, and describes training on
  964 bird species from a subset of iNaturalist 2017. This is model-specific
  evidence, in addition to Coral test_data's repository licence.
- Apache 2.0 permits redistribution and modification, including associated-file
  repackaging, subject to section 4: supply the licence, identify modifications,
  retain applicable attribution, and retain upstream NOTICE attribution if
  present. No separate model licence or upstream NOTICE was present in the
  inspected Coral distribution. Its COPYRIGHT file lists exceptions for other
  assets, not this model. No training images are redistributed.
- `licenses/Apache-2.0.txt` preserves Coral's licence text (trailing whitespace
  removed). `aiy-v1.NOTICE.txt` records attribution and the packaging changes.
  Both are embedded as associated `LICENSE` and `NOTICE` files so the single
  downloadable model carries its licence and notices.

Labels and compatibility:

- Both sets have 965 entries with background at index 964. Scientific-name
  ordering matches at every index. Coral's label text at indexes 110 and 758
  contains literal `\xd7`; WAMF uses U+00D7. Google's [official AIY label map](https://www.gstatic.com/aihub/tfhub/labelmaps/aiy_birds_V1_labelmap.csv)
  agrees with all 964 WAMF scientific names, including those two corrections.
  Its SHA-256 is `26925be64bc1d10bfc0c23c8619cb71e0464e7ad3d4d446f805fc60edc8b59de`.
- WAMF's existing embedded display/scientific and category labels are copied
  byte-for-byte. Their SHA-256 values are pinned in the packaging script.
  No class is reordered or remapped. The original upstream associated
  `inat_bird_labels.txt` remains unchanged and accessible.
- One uint8 `[1,224,224,3]` input, scale `0.0078125`, zero point `128`;
  one uint8 `[1,965]` output, scale `0.00390625`, zero point `0`.
  WAMF's RGB/direct-resize/raw-uint8 contract is unchanged.

Reproduce offline after acquiring the pinned upstream CPU model:

```sh
.venv312/bin/python scripts/package_aiy_birds.py /path/to/mobilenet_v2_1.0_224_inat_bird_quant.tflite /path/to/bird-aiy-v1.tflite
```

The script verifies upstream and bundled-label identities, preserves the entire
upstream byte prefix, and appends deterministic uncompressed ZIP associated
files with fixed timestamps and permissions. It performs no fetching,
installation, activation or catalogue edits. Output paths must not exist.

Final artifact: `bird-aiy-v1.tflite`, 3,621,418 bytes, SHA-256
`1dea79841cd7d767515c928ed5010ec49df57bef37aaa894f70b44eaa45d1cab`.
Published under the dedicated `models-aiy-v1` release in `WAMF-Project/WAMF`;
the catalogue records its fixed release-asset URL. Binaries stay outside Git.

Before publication, M1 validation (including isolated synthetic inference)
passed, a second build was byte-identical, and raw versus packaged outputs
were byte-identical for deterministic ramp, zero and constant-128 RGB tensors.
These checks establish packaging compatibility, not classification accuracy.
