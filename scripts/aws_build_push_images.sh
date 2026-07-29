#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TF_DIR="$ROOT_DIR/infra/aws/terraform"
AWS_REGION="${AWS_REGION:-us-east-1}"
IMAGE_TAG="${IMAGE_TAG:-$(git -C "$ROOT_DIR" rev-parse HEAD)}"
BUILD_RERANKER="${BUILD_RERANKER:-false}"

core_repo="$(terraform -chdir="$TF_DIR" output -raw core_ecr_repository_url)"
frontend_repo="$(terraform -chdir="$TF_DIR" output -raw frontend_ecr_repository_url)"
registry="${core_repo%%/*}"
aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "$registry" >/dev/null

image_exists() {
  local repository_url="$1"
  local repository_name="${repository_url#*/}"
  aws ecr describe-images \
    --region "$AWS_REGION" \
    --repository-name "$repository_name" \
    --image-ids "imageTag=$IMAGE_TAG" >/dev/null 2>&1
}

if image_exists "$core_repo"; then
  printf 'Core image %s already exists; preserving immutable tag.\n' "$IMAGE_TAG"
else
  docker build --pull -t "$core_repo:$IMAGE_TAG" "$ROOT_DIR"
  docker push "$core_repo:$IMAGE_TAG"
fi

if image_exists "$frontend_repo"; then
  printf 'Frontend image %s already exists; preserving immutable tag.\n' "$IMAGE_TAG"
else
  docker build --pull --build-arg NEXT_PUBLIC_API_BASE_URL=/api -t "$frontend_repo:$IMAGE_TAG" "$ROOT_DIR/apps/web"
  docker push "$frontend_repo:$IMAGE_TAG"
fi

if [[ "$BUILD_RERANKER" == "true" ]]; then
  : "${RERANKER_MODEL_REVISION:?RERANKER_MODEL_REVISION is required when BUILD_RERANKER=true}"
  reranker_repo="$(terraform -chdir="$TF_DIR" output -raw reranker_ecr_repository_url)"
  if image_exists "$reranker_repo"; then
    printf 'Reranker image %s already exists; preserving immutable tag.\n' "$IMAGE_TAG"
  else
    docker build --pull \
      --build-arg MODEL_REVISION="$RERANKER_MODEL_REVISION" \
      --build-arg RERANKER_MODEL_ID="${RERANKER_MODEL_ID:-BAAI/bge-reranker-v2-m3}" \
      -t "$reranker_repo:$IMAGE_TAG" "$ROOT_DIR/model_runtime"
    docker push "$reranker_repo:$IMAGE_TAG"
  fi
fi

printf 'Pushed immutable image tag %s\n' "$IMAGE_TAG"
