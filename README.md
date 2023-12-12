# modal-sqlcoder
 Deploy defog sqlcoder2 on Modal using Text Generation Inference (TGI)

Adapted from:
- https://github.com/modal-labs/modal-examples/blob/main/06_gpu_and_ml/text_generation_inference.py
- https://github.com/defog-ai/sqlcoder

## Set up environment
Set up your `HUGGING_FACE_HUB_TOKEN` environment variable in a Modal Secret named `huggingface`.

## Serve the model
Deploy this model with 
```
$ modal deploy sql_generation_inference.py
```


## Invoke the model from other apps
Once the model is deployed, we can invoke inference from other apps, sharing the same pool
of GPU containers with all other apps we might need.

 ```
$ python
>>> import modal
>>> f = modal.Function.lookup("example-tgi-sqlcoder2", "Model.generate")
>>> result = f.remote("How many salespeople are there?", metadata="(Replace with your own metadata)")
 ```
