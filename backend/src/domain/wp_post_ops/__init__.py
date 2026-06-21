"""Update / delete уже опубликованного поста на живом сайте."""

from .service import delete_remote_post, update_remote_post

__all__ = ["update_remote_post", "delete_remote_post"]
