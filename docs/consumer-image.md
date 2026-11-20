# How to build a consumer image

A consumer is the service that posts jobs. Its image needs only the client library, a plain pip package (`httpx`, `fsspec`; extras `mint-s3` = boto3, `mint-gcs` = google-auth):

```dockerfile
ADD lib/cadmus-parser /cadmus
RUN pip install /cadmus/client            # or "/cadmus/client[mint-s3]" to mint S3 credentials
```

No base image, no container setup. The parser service runs in a separate image: [How to build the parser image](parser-image.md); the stack file and overlay wire the two together
([reference](reference.md#stack-file)).
