"""
publisher.py - Multi-Platform Article Publisher
Supports publishing Markdown articles to:
- Dev.to (REST API)
- Medium (REST API)
- Hashnode (GraphQL API)
- WordPress (REST API)
- Custom Webhook (JSON payload for headless CMS / custom sites)
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional
import requests


class PublishResult:
    def __init__(
        self,
        platform: str,
        success: bool,
        url: Optional[str] = None,
        post_id: Optional[str] = None,
        error: Optional[str] = None,
    ):
        self.platform = platform
        self.success = success
        self.url = url
        self.post_id = post_id
        self.error = error

    def to_dict(self) -> Dict[str, Any]:
        return {
            "platform": self.platform,
            "success": self.success,
            "url": self.url,
            "post_id": self.post_id,
            "error": self.error,
        }


class ArticlePublisher:
    """Handles publishing markdown articles across multiple blogging platforms."""

    @staticmethod
    def extract_title(markdown: str, fallback: str = "Technical Article") -> str:
        """Extract first H1 heading from Markdown or use fallback."""
        lines = markdown.strip().splitlines()
        for line in lines:
            if line.startswith("# "):
                return line.replace("# ", "").strip()
        return fallback

    # ---------------------------------------------------------
    # 1. Dev.to
    # ---------------------------------------------------------
    @classmethod
    def publish_to_devto(
        cls,
        title: str,
        markdown_body: str,
        api_key: Optional[str] = None,
        tags: Optional[List[str]] = None,
        published: bool = False,
        canonical_url: Optional[str] = None,
        series: Optional[str] = None,
    ) -> PublishResult:
        """
        Publish to Dev.to via REST API:
        https://developers.forem.com/api/v1#tag/articles/operation/createArticle
        """
        key = api_key or os.getenv("DEVTO_API_KEY")
        if not key:
            return PublishResult(
                platform="devto",
                success=False,
                error="Missing Dev.to API Key. Set DEVTO_API_KEY in .env or provide api_key.",
            )

        clean_tags = [re.sub(r"[^a-zA-Z0-9]", "", t).lower() for t in (tags or ["ai", "python", "tech"])][:4]

        payload = {
            "article": {
                "title": title,
                "body_markdown": markdown_body,
                "published": published,
                "tags": clean_tags,
            }
        }
        if canonical_url:
            payload["article"]["canonical_url"] = canonical_url
        if series:
            payload["article"]["series"] = series

        headers = {
            "api-key": key,
            "Content-Type": "application/json",
            "User-Agent": "AgentWriter-AI/1.0",
        }

        try:
            resp = requests.post(
                "https://dev.to/api/articles",
                json=payload,
                headers=headers,
                timeout=15,
            )
            if resp.status_code in (200, 201):
                data = resp.json()
                return PublishResult(
                    platform="devto",
                    success=True,
                    url=data.get("url") or f"https://dev.to/{data.get('slug', '')}",
                    post_id=str(data.get("id", "")),
                )
            return PublishResult(
                platform="devto",
                success=False,
                error=f"Dev.to returned HTTP {resp.status_code}: {resp.text}",
            )
        except Exception as exc:
            return PublishResult(platform="devto", success=False, error=str(exc))

    # ---------------------------------------------------------
    # 2. Medium
    # ---------------------------------------------------------
    @classmethod
    def publish_to_medium(
        cls,
        title: str,
        markdown_body: str,
        integration_token: Optional[str] = None,
        tags: Optional[List[str]] = None,
        publish_status: str = "draft",  # 'public', 'draft', 'unlisted'
        canonical_url: Optional[str] = None,
        publication_id: Optional[str] = None,
    ) -> PublishResult:
        """
        Publish to Medium via official REST API:
        https://github.com/Medium/medium-api-docs
        """
        token = integration_token or os.getenv("MEDIUM_INTEGRATION_TOKEN")
        if not token:
            return PublishResult(
                platform="medium",
                success=False,
                error="Missing Medium Integration Token. Set MEDIUM_INTEGRATION_TOKEN in .env or provide integration_token.",
            )

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        try:
            if not publication_id:
                user_resp = requests.get(
                    "https://api.medium.com/v1/me",
                    headers=headers,
                    timeout=15,
                )
                if user_resp.status_code != 200:
                    return PublishResult(
                        platform="medium",
                        success=False,
                        error=f"Failed to fetch Medium user: {user_resp.text}",
                    )
                user_id = user_resp.json().get("data", {}).get("id")
                post_endpoint = f"https://api.medium.com/v1/users/{user_id}/posts"
            else:
                post_endpoint = f"https://api.medium.com/v1/publications/{publication_id}/posts"

            payload = {
                "title": title,
                "contentFormat": "markdown",
                "content": f"# {title}\n\n{markdown_body}" if not markdown_body.strip().startswith("# ") else markdown_body,
                "tags": (tags or ["Artificial Intelligence", "Technology", "Programming"])[:5],
                "publishStatus": publish_status if publish_status in ("public", "draft", "unlisted") else "draft",
            }
            if canonical_url:
                payload["canonicalUrl"] = canonical_url

            post_resp = requests.post(
                post_endpoint,
                json=payload,
                headers=headers,
                timeout=20,
            )
            if post_resp.status_code in (200, 201):
                data = post_resp.json().get("data", {})
                return PublishResult(
                    platform="medium",
                    success=True,
                    url=data.get("url"),
                    post_id=str(data.get("id", "")),
                )
            return PublishResult(
                platform="medium",
                success=False,
                error=f"Medium API error {post_resp.status_code}: {post_resp.text}",
            )
        except Exception as exc:
            return PublishResult(platform="medium", success=False, error=str(exc))

    # ---------------------------------------------------------
    # 3. Hashnode
    # ---------------------------------------------------------
    @classmethod
    def publish_to_hashnode(
        cls,
        title: str,
        markdown_body: str,
        api_token: Optional[str] = None,
        publication_id: Optional[str] = None,
        tags: Optional[List[str]] = None,
        subtitle: Optional[str] = None,
        slug: Optional[str] = None,
    ) -> PublishResult:
        """
        Publish to Hashnode via GraphQL API (Hashnode API v2):
        https://apidocs.hashnode.com/
        """
        token = api_token or os.getenv("HASHNODE_API_TOKEN")
        pub_id = publication_id or os.getenv("HASHNODE_PUBLICATION_ID")

        if not token or not pub_id:
            return PublishResult(
                platform="hashnode",
                success=False,
                error="Missing Hashnode API token or publication ID.",
            )

        endpoint = "https://gql.hashnode.com"
        headers = {
            "Authorization": token,
            "Content-Type": "application/json",
        }

        mutation = """
        mutation PublishPost($input: PublishPostInput!) {
            publishPost(input: $input) {
                post {
                    id
                    title
                    url
                    slug
                }
            }
        }
        """

        clean_slug = slug or re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
        tags_input = [{"slug": re.sub(r"[^a-z0-9]", "", t).lower(), "name": t} for t in (tags or ["tech", "ai"])[:5]]

        variables = {
            "input": {
                "title": title,
                "subtitle": subtitle or "",
                "publicationId": pub_id,
                "contentMarkdown": markdown_body,
                "slug": clean_slug,
                "tags": tags_input,
            }
        }

        try:
            resp = requests.post(
                endpoint,
                json={"query": mutation, "variables": variables},
                headers=headers,
                timeout=20,
            )
            if resp.status_code == 200:
                body = resp.json()
                if "errors" in body and body["errors"]:
                    return PublishResult(
                        platform="hashnode",
                        success=False,
                        error=str(body["errors"][0].get("message")),
                    )
                post = body.get("data", {}).get("publishPost", {}).get("post", {})
                return PublishResult(
                    platform="hashnode",
                    success=True,
                    url=post.get("url"),
                    post_id=post.get("id"),
                )
            return PublishResult(
                platform="hashnode",
                success=False,
                error=f"Hashnode API HTTP {resp.status_code}: {resp.text}",
            )
        except Exception as exc:
            return PublishResult(platform="hashnode", success=False, error=str(exc))

    # ---------------------------------------------------------
    # 4. WordPress (REST API)
    # ---------------------------------------------------------
    @classmethod
    def publish_to_wordpress(
        cls,
        title: str,
        content: str,
        site_url: Optional[str] = None,
        username: Optional[str] = None,
        app_password: Optional[str] = None,
        status: str = "draft",  # 'draft' or 'publish'
    ) -> PublishResult:
        """
        Publish to WordPress via REST API (/wp-json/wp/v2/posts)
        Uses WordPress Application Passwords for authentication.
        """
        url = (site_url or os.getenv("WORDPRESS_URL", "")).rstrip("/")
        user = username or os.getenv("WORDPRESS_USERNAME")
        password = app_password or os.getenv("WORDPRESS_APP_PASSWORD")

        if not url or not user or not password:
            return PublishResult(
                platform="wordpress",
                success=False,
                error="Missing WordPress credentials (site URL, username, application password).",
            )

        api_endpoint = f"{url}/wp-json/wp/v2/posts"

        payload = {
            "title": title,
            "content": content,
            "status": status if status in ("draft", "publish") else "draft",
        }

        try:
            resp = requests.post(
                api_endpoint,
                json=payload,
                auth=(user, password),
                headers={"Content-Type": "application/json"},
                timeout=20,
            )
            if resp.status_code in (200, 201):
                data = resp.json()
                return PublishResult(
                    platform="wordpress",
                    success=True,
                    url=data.get("link"),
                    post_id=str(data.get("id", "")),
                )
            return PublishResult(
                platform="wordpress",
                success=False,
                error=f"WordPress returned {resp.status_code}: {resp.text}",
            )
        except Exception as exc:
            return PublishResult(platform="wordpress", success=False, error=str(exc))

    # ---------------------------------------------------------
    # 5. Custom Webhook (Headless CMS / Custom Server)
    # ---------------------------------------------------------
    @classmethod
    def publish_to_webhook(
        cls,
        title: str,
        markdown_body: str,
        webhook_url: Optional[str] = None,
        secret_token: Optional[str] = None,
        extra_meta: Optional[Dict[str, Any]] = None,
    ) -> PublishResult:
        """
        Publish to custom webhook URL with standard JSON payload.
        """
        url = webhook_url or os.getenv("PUBLISH_WEBHOOK_URL")
        if not url:
            return PublishResult(
                platform="webhook",
                success=False,
                error="Missing webhook URL.",
            )

        headers = {"Content-Type": "application/json"}
        if secret_token:
            headers["Authorization"] = f"Bearer {secret_token}"

        payload = {
            "title": title,
            "markdown": markdown_body,
            "meta": extra_meta or {},
        }

        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=20)
            if resp.status_code in (200, 201, 202, 204):
                try:
                    data = resp.json()
                    post_url = data.get("url") or data.get("link")
                except Exception:
                    post_url = url
                return PublishResult(
                    platform="webhook",
                    success=True,
                    url=post_url,
                    post_id=None,
                )
            return PublishResult(
                platform="webhook",
                success=False,
                error=f"Webhook returned {resp.status_code}: {resp.text}",
            )
        except Exception as exc:
            return PublishResult(platform="webhook", success=False, error=str(exc))

    # ---------------------------------------------------------
    # Unified Multi-Platform Dispatcher
    # ---------------------------------------------------------
    @classmethod
    def publish_multi(
        cls,
        title: str,
        markdown_body: str,
        platforms_config: Dict[str, Dict[str, Any]],
    ) -> Dict[str, Dict[str, Any]]:
        results: Dict[str, Dict[str, Any]] = {}

        for platform, config in platforms_config.items():
            if not config.get("enabled", True):
                continue

            if platform == "devto":
                res = cls.publish_to_devto(
                    title=title,
                    markdown_body=markdown_body,
                    api_key=config.get("api_key"),
                    tags=config.get("tags"),
                    published=config.get("published", False),
                    canonical_url=config.get("canonical_url"),
                    series=config.get("series"),
                )
            elif platform == "medium":
                res = cls.publish_to_medium(
                    title=title,
                    markdown_body=markdown_body,
                    integration_token=config.get("integration_token"),
                    tags=config.get("tags"),
                    publish_status=config.get("publish_status", "draft"),
                    canonical_url=config.get("canonical_url"),
                    publication_id=config.get("publication_id"),
                )
            elif platform == "hashnode":
                res = cls.publish_to_hashnode(
                    title=title,
                    markdown_body=markdown_body,
                    api_token=config.get("api_token"),
                    publication_id=config.get("publication_id"),
                    tags=config.get("tags"),
                    subtitle=config.get("subtitle"),
                    slug=config.get("slug"),
                )
            elif platform == "wordpress":
                res = cls.publish_to_wordpress(
                    title=title,
                    content=markdown_body,
                    site_url=config.get("site_url"),
                    username=config.get("username"),
                    app_password=config.get("app_password"),
                    status=config.get("status", "draft"),
                )
            elif platform == "webhook":
                res = cls.publish_to_webhook(
                    title=title,
                    markdown_body=markdown_body,
                    webhook_url=config.get("webhook_url"),
                    secret_token=config.get("secret_token"),
                    extra_meta=config.get("extra_meta"),
                )
            else:
                res = PublishResult(
                    platform=platform,
                    success=False,
                    error=f"Unsupported platform: {platform}",
                )

            results[platform] = res.to_dict()

        return results
