"""blog URL'leri — /api/v1/blog/..."""
from django.urls import path

from .views import BlogCategoryListView, BlogPostDetailView, BlogPostListView

urlpatterns = [
    path("posts/", BlogPostListView.as_view(), name="blog-posts"),
    path("posts/<slug:slug>/", BlogPostDetailView.as_view(), name="blog-post-detail"),
    path("categories/", BlogCategoryListView.as_view(), name="blog-categories"),
]
