from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List
from uuid import uuid4

from langchain_community.document_loaders import PyPDFLoader
from langchain_openai import OpenAIEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.core.config import DocumentProcessingSettings, get_settings
from app.core.secrets import fetch_secret
from app.exception import ServerException
from app.logger import get_logger

logger = get_logger(__name__)


@dataclass
class ChunkEmbedding:
    """Represents a single chunk of text and its embedding vector."""

    chunk_id: str
    text: str
    embedding: List[float]
    metadata: Dict[str, Any]


class PDFEmbeddingPipeline:
    """Load, chunk, and embed PDFs using LangChain components."""

    def __init__(self) -> None:
        settings = get_settings()
        self.processing: DocumentProcessingSettings = settings.processing
        self.splitter = self._build_splitter()
        self.embedder = self._build_embedder()

    def _build_embedder(self):
        provider = self.processing.embedding_provider.lower()

        if provider == "openai":
            settings = get_settings()
            api_key = fetch_secret(
                settings.openai_secret_name, settings.openai_secret_version
            )
            if not api_key:
                raise ServerException(
                    "OpenAI API key secret returned no data.",
                    code="embedding_configuration_error",
                    detail={"provider": provider, "secret": settings.openai_secret_name},
                )
            return OpenAIEmbeddings(
                api_key=api_key,
                model=self.processing.embedding_model,
            )

        raise ServerException(
            f"Unsupported embedding provider '{provider}'.",
            code="embedding_configuration_error",
            detail={"provider": provider},
        )

    def _build_splitter(
        self,
        *,
        chunk_size: int | None = None,
        chunk_overlap: int | None = None,
    ) -> RecursiveCharacterTextSplitter:
        return RecursiveCharacterTextSplitter(
            chunk_size=chunk_size or self.processing.chunk_size,
            chunk_overlap=chunk_overlap
            if chunk_overlap is not None
            else self.processing.chunk_overlap,
            separators=["\n\n", "\n", " ", ""],
        )

    def process(
        self,
        pdf_path: Path,
        *,
        context: Dict[str, Any],
        chunk_config: Dict[str, int] | None = None,
    ) -> List[ChunkEmbedding]:
        """
        Run the end-to-end pipeline: load PDF, chunk text, and embed chunks.

        Args:
            pdf_path: Path to the persisted PDF file.
            context: Metadata to propagate into chunk metadata (request_id, tenant_id, etc.).
        """
        documents = self._load_pdf(pdf_path)
        if not documents:
            raise ServerException(
                "Uploaded PDF did not yield any readable pages.",
                code="empty_document",
                detail={"source_path": str(pdf_path)},
            )

        splitter = (
            self._build_splitter(
                chunk_size=chunk_config.get("size"),
                chunk_overlap=chunk_config.get("overlap"),
            )
            if chunk_config
            else self.splitter
        )

        chunks = splitter.split_documents(documents)

        texts = [chunk.page_content for chunk in chunks]
        embeddings = self.embedder.embed_documents(texts)

        chunk_embeddings: List[ChunkEmbedding] = []
        for index, (chunk, vector) in enumerate(zip(chunks, embeddings)):
            metadata = {
                **chunk.metadata,
                **context,
                "chunk_index": index,
                "source_path": str(pdf_path),
            }
            chunk_embeddings.append(
                ChunkEmbedding(
                    chunk_id=uuid4().hex,
                    text=chunk.page_content,
                    embedding=vector,
                    metadata=metadata,
                )
            )

        logger.info(
            "Generated chunk embeddings.",
            extra={
                "source_path": str(pdf_path),
                "chunk_count": len(chunk_embeddings),
                "model": self.processing.embedding_model,
            },
        )
        return chunk_embeddings

    @staticmethod
    def _load_pdf(pdf_path: Path):
        if not pdf_path.exists():
            raise ServerException(
                "Uploaded file missing before processing could begin.",
                code="missing_temp_file",
                detail={"source_path": str(pdf_path)},
            )

        loader = PyPDFLoader(str(pdf_path))
        try:
            return loader.load()
        except Exception as exc:  # noqa: BLE001 - surface parsing errors
            logger.exception(
                "Failed to parse PDF for embedding.",
                extra={"source_path": str(pdf_path)},
            )
            raise ServerException(
                "Unable to read PDF contents for embedding.",
                code="pdf_parse_error",
                detail={"source_path": str(pdf_path)},
            ) from exc
