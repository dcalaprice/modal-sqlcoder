# # Hosting the SQLCoder2 LLM with Text Generation Inference (TGI)
#
# Adapted from:
#   https://github.com/modal-labs/modal-examples/blob/main/06_gpu_and_ml/text_generation_inference.py
#   https://github.com/defog-ai/sqlcoder
#
# In this example, we show how to run an optimized inference server using [Text Generation Inference (TGI)](https://github.com/huggingface/text-generation-inference)
# with performance advantages over standard text generation pipelines including:
# - continuous batching, so multiple generations can take place at the same time on a single container
# - PagedAttention, an optimization that increases throughput.
#

# ## Setup
#
# First we import the components we need from `modal`.

from modal import Image, Mount, Secret, Stub, asgi_app, gpu, method

# Next, we set which model to serve, taking care to specify the GPU configuration required
# to fit the model into VRAM, and the quantization method (`bitsandbytes` or `gptq`) if desired.
# Note that quantization does degrade token generation performance significantly.
#
# Any model supported by TGI can be chosen here.

GPU_CONFIG = gpu.A100(memory=40, count=1)
MODEL_ID = "defog/sqlcoder2"
REVISION = "4ccba9158b67de83b070a4eb2fadaeb58ab2cd14"
# Add `["--quantize", "gptq"]` for TheBloke GPTQ models.
LAUNCH_FLAGS = [
    "--model-id",
    MODEL_ID,
    "--port",
    "8000",
    "--revision",
    REVISION,
]

# ## Define a container image
#
# We want to create a Modal image which has the Huggingface model cache pre-populated.
# The benefit of this is that the container no longer has to re-download the model from Huggingface -
# instead, it will take advantage of Modal's internal filesystem for faster cold starts. 
#
# ### Download the weights
# We can use the included utilities to download the model weights (and convert to safetensors, if necessary)
# as part of the image build.
#

def download_model():
    import subprocess

    subprocess.run(
        [
            "text-generation-server",
            "download-weights",
            MODEL_ID,
            "--revision",
            REVISION,
        ]
    )


# ### Image definition
# We’ll start from a Dockerhub image recommended by TGI, and override the default `ENTRYPOINT` for
# Modal to run its own which enables seamless serverless deployments.
#
# Next we run the download step to pre-populate the image with our model weights.
#
# For this step to work on a gated model such as LLaMA 2, the HUGGING_FACE_HUB_TOKEN environment
# variable must be set ([reference](https://github.com/huggingface/text-generation-inference#using-a-private-or-gated-model)).
# After [creating a HuggingFace access token](https://huggingface.co/settings/tokens),
# head to the [secrets page](https://modal.com/secrets) to create a Modal secret.
#
# The key should be `HUGGING_FACE_HUB_TOKEN` and the value should be your access token.
#
# Finally, we install the `text-generation` client to interface with TGI's Rust webserver over `localhost`.

print(f"Image: {dir(Image)}")

image = (
    Image.from_registry("ghcr.io/huggingface/text-generation-inference:1.0.3")
    .dockerfile_commands("ENTRYPOINT []")
    .run_function(download_model, secret=Secret.from_name("huggingface"))
    .pip_install("text-generation")
)

stub = Stub("example-tgi-" + MODEL_ID.split("/")[-1], image=image)


# ## The model class
#
# The inference function is best represented with Modal's [class syntax](/docs/guide/lifecycle-functions).
# The class syntax is a special representation for a Modal function which splits logic into two parts:
# 1. the `__enter__` method, which runs once per container when it starts up, and
# 2. the `@method()` function, which runs per inference request.
#
# This means the model is loaded into the GPUs, and the backend for TGI is launched just once when each
# container starts, and this state is cached for each subsequent invocation of the function.
# Note that on start-up, we must wait for the Rust webserver to accept connections before considering the
# container ready.
#
# Here, we also
# - specify the secret so the `HUGGING_FACE_HUB_TOKEN` environment variable is set
# - specify how many A100s we need per container
# - specify that each container is allowed to handle up to 10 inputs (i.e. requests) simultaneously
# - keep idle containers for 10 minutes before spinning down
# - lift the timeout of each request.


@stub.cls(
    secret=Secret.from_name("huggingface"),
    gpu=GPU_CONFIG,
    allow_concurrent_inputs=10,
    container_idle_timeout=60 * 10,
    timeout=60 * 60,
)
class Model:
    def __enter__(self):
        import socket
        import subprocess
        import time

        from text_generation import AsyncClient

        self.launcher = subprocess.Popen(
            ["text-generation-launcher"] + LAUNCH_FLAGS
        )
        self.client = AsyncClient("http://127.0.0.1:8000", timeout=60)

        # Poll until webserver at 127.0.0.1:8000 accepts connections before running inputs.
        def webserver_ready():
            try:
                socket.create_connection(("127.0.0.1", 8000), timeout=1).close()
                return True
            except (socket.timeout, ConnectionRefusedError):
                # Check if launcher webserving process has exited.
                # If so, a connection can never be made.
                retcode = self.launcher.poll()
                if retcode is not None:
                    raise RuntimeError(
                        f"launcher exited unexpectedly with code {retcode}"
                    )
                return False

        while not webserver_ready():
            time.sleep(1.0)

        print("Webserver ready!")

    def __exit__(self, _exc_type, _exc_value, _traceback):
        self.launcher.terminate()

    @method()
    async def generate(self, question: str, metadata: str):
        print("Generating...")
        prompt = generate_prompt(question, metadata=metadata)
        result = await self.client.generate(prompt, max_new_tokens=1024)

        print("Generated!")
        print(f"Result: {result.generated_text}")

        return result.generated_text

    @method()
    async def generate_stream(self, question: str, metadata: str):
        prompt = generate_prompt(question, metadata=metadata)
        async for response in self.client.generate_stream(
            prompt, max_new_tokens=1024
        ):
            if not response.token.special:
                yield response.token.text


# ## Run the model
# We define a [`local_entrypoint`](/docs/guide/apps#entrypoints-for-ephemeral-apps) to invoke
# our remote function. You can run this script locally with `modal run text_generation_inference.py`.
# This entrypoint generates a response using an example query and default metadata.
@stub.local_entrypoint()
def main():
    print("main() started")
    result =  Model().generate.remote("Do we get more revenue from customers in New York compared to customers in San Francisco? Give me the total revenue for each city, and the difference between the two.", metadata=METADATA_DEFAULT)
    print(f"main() result: {result}")
    
PROMPT_TEMPLATE = """### Task
Generate a SQL query to answer the following question:
`{user_question}`

### Database Schema
The query will run on a database with the following schema:
{table_metadata_string}

### SQL
Follow these steps to create the SQL Query:
1. Only use the columns and tables present in the database schema
2. Use table aliases to prevent ambiguity when doing joins. For example, `SELECT table1.col1, table2.col1 FROM table1 JOIN table2 ON table1.id = table2.id`.

Given the database schema, here is the SQL query that answers `{user_question}`:
```sql
"""

METADATA_DEFAULT = """CREATE TABLE products (
  product_id INTEGER PRIMARY KEY, -- Unique ID for each product
  name VARCHAR(50), -- Name of the product
  price DECIMAL(10,2), -- Price of each unit of the product
  quantity INTEGER  -- Current quantity in stock
);

CREATE TABLE customers (
   customer_id INTEGER PRIMARY KEY, -- Unique ID for each customer
   name VARCHAR(50), -- Name of the customer
   address VARCHAR(100) -- Mailing address of the customer
);

CREATE TABLE salespeople (
  salesperson_id INTEGER PRIMARY KEY, -- Unique ID for each salesperson 
  name VARCHAR(50), -- Name of the salesperson
  region VARCHAR(50) -- Geographic sales region 
);

CREATE TABLE sales (
  sale_id INTEGER PRIMARY KEY, -- Unique ID for each sale
  product_id INTEGER, -- ID of product sold
  customer_id INTEGER,  -- ID of customer who made purchase
  salesperson_id INTEGER, -- ID of salesperson who made the sale
  sale_date DATE, -- Date the sale occurred 
  quantity INTEGER -- Quantity of product sold
);

CREATE TABLE product_suppliers (
  supplier_id INTEGER PRIMARY KEY, -- Unique ID for each supplier
  product_id INTEGER, -- Product ID supplied
  supply_price DECIMAL(10,2) -- Unit price charged by supplier
);

-- sales.product_id can be joined with products.product_id
-- sales.customer_id can be joined with customers.customer_id 
-- sales.salesperson_id can be joined with salespeople.salesperson_id
-- product_suppliers.product_id can be joined with products.product_id
"""

def generate_prompt(question, prompt_template=PROMPT_TEMPLATE, metadata=METADATA_DEFAULT):
    print(f"generate_prompt() Question: {question}") 
    prompt = prompt_template.format(
        user_question=question, table_metadata_string=metadata
    )
    print(f"generate_prompt() Prompt: {prompt}")
    return prompt

# ## Serve the model
# Deploy this model with `modal deploy sql_generation_inference.py`


# ## Invoke the model from other apps
# Once the model is deployed, we can invoke inference from other apps, sharing the same pool
# of GPU containers with all other apps we might need.
#
# ```
# $ python
# >>> import modal
# >>> f = modal.Function.lookup("example-tgi-sqlcoder2", "Model.generate")
# >>> result = f.remote("How many salespeople are there?", metadata=metadata)
# ```
