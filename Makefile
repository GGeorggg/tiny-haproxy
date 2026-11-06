IMAGE      ?= tiny-haproxy:dev
CERT_DIR   ?= .test-certs
HADOLINT   ?= hadolint/hadolint:v2.12.0
ACTIONLINT ?= rhysd/actionlint:1.7.7
TRIVY      ?= aquasec/trivy:0.58.2

.PHONY: help lint build certs check test scan all clean
help: ## list targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*##/ -/'

lint: ## hadolint, actionlint, shellcheck
	docker run --rm -i $(HADOLINT) < Dockerfile
	docker run --rm -v "$(CURDIR):/repo" -w /repo $(ACTIONLINT)
	shellcheck scripts/*.sh .devcontainer/*.sh

build: ## build image for the local platform
	docker build -t $(IMAGE) .

certs: ## throw-away test certificates
	scripts/gen-test-certs.sh $(CERT_DIR)

check: build certs ## haproxy -c against the built image
	docker run --rm --entrypoint sh \
	  -v "$(CURDIR)/$(CERT_DIR)/site.pem:/etc/haproxy/certs/site.pem:ro" \
	  -v "$(CURDIR)/$(CERT_DIR)/ca.crt:/etc/haproxy/certs/ca.crt:ro" \
	  $(IMAGE) -c 'assemble-config.sh && haproxy -c -f /usr/local/etc/haproxy/haproxy.cfg'

test: build ## behavioural smoke test
	scripts/smoke-test.sh $(IMAGE)

scan: build ## trivy: HIGH/CRITICAL with available fix
	docker run --rm -v /var/run/docker.sock:/var/run/docker.sock $(TRIVY) image \
	  --severity HIGH,CRITICAL --ignore-unfixed --exit-code 1 $(IMAGE)

all: lint check test scan ## everything CI does

clean:
	rm -rf $(CERT_DIR)
