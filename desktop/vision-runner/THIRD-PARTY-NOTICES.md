# TrafficSuperVision command proposal runner notices

The bundled review-only command proposal runner uses the following components.

- `onnx-community/grounding-dino-tiny-ONNX`
  - Revision: `11778337bb87ff84af72a64928c86616d86c495e`
  - Selected artifact: `onnx/model_q4.onnx`
  - Artifact SHA-256:
    `2ed98a91b75f00d325c7581bee83b92b5b98460981772edf5ed844370ac25c11`
  - License: Apache-2.0
  - Source: <https://huggingface.co/onnx-community/grounding-dino-tiny-ONNX>
- ONNX Runtime, NumPy, Pillow, and Tokenizers are frozen into the runner
  executable. Their package licenses remain applicable.

TrafficSuperVision treats every output as a model score and a review-only object
proposal. It is not a defect probability, a maintenance decision, an approved
label, or an incident.
