FROM node:22-alpine AS build
WORKDIR /app
ENV npm_config_registry=https://registry.npmmirror.com
COPY package.json ./
RUN npm install
COPY . .
RUN npm run build

FROM nginx:alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
