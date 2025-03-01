VERSION?=$$(git rev-parse --abbrev-ref HEAD)

.PHONY: all
all: dockerize

.PHONY: bump
bump: bump-requirements

poetry.lock:
	poetry install

.PHONY: bump-poetry-lock
bump-poetry-lock:
	poetry update

.PHONY: bump-requirements
bump-requirements: bump-poetry-lock

.PHONY: dockerize
dockerize:
	docker build --tag hiboxsystems/marge-bot:$$(cat version) .

.PHONY: docker-push
docker-push:
	if [ -n "$$DOCKER_USERNAME" -a -n "$$DOCKER_PASSWORD" ]; then \
		docker login -u "$${DOCKER_USERNAME}" -p "$${DOCKER_PASSWORD}"; \
	else \
		docker login; \
	fi
	docker tag hiboxsystems/marge-bot:$$(cat version) hiboxsystems/marge-bot:$(VERSION)
	if [ "$(VERSION)" = "$$(cat version)" ]; then \
		docker tag hiboxsystems/marge-bot:$$(cat version) hiboxsystems/marge-bot:latest; \
		docker tag hiboxsystems/marge-bot:$$(cat version) hiboxsystems/marge-bot:stable; \
		docker push hiboxsystems/marge-bot:stable; \
		docker push hiboxsystems/marge-bot:latest; \
	fi
	docker push hiboxsystems/marge-bot:$(VERSION)
