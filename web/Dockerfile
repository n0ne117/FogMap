# syntax=docker/dockerfile:1
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Build context is the repository root so VERSION can be copied in and baked
# into the bundle by Vite. There is no dev server and no hot reload - frontend
# iteration costs a full build cycle, which is why the frontend stays small.

FROM node:22-alpine AS build
WORKDIR /app
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
COPY VERSION ./VERSION
RUN npm run build

FROM nginx:1.27-alpine AS runtime
COPY web/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
