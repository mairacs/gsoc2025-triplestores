# Copyright (C) 2025 Maira Papadopoulou
# SPDX-License-Identifier: Apache-2.0

import logging
from pathlib import Path
from typing import Any

import requests

from triplestore.base import TriplestoreBackend
from triplestore.utils import validate_config

logger = logging.getLogger(__name__)


class RDF4J(TriplestoreBackend):
    """
    A triplestore backend implementation for Eclipse RDF4J Server using its HTTP REST API.
    """

    REQUIRED_KEYS = {"name"}
    OPTIONAL_DEFAULTS = {
        "base_url": "http://localhost:8080/rdf4j-server",
        "graph": None,
        "auth": None,
        "store_type": "native",
    }
    ALIASES = {
        "graph_uri": "graph",
        "repository": "name",
    }

    def __init__(self, config: dict[str, Any]) -> None:
        """
        Initialize the RDF4J backend with the given configuration.

        Parameters
        ----------
        config : dict
            A configuration dictionary containing connection parameters:
            - base_url (optional): The base URL of the RDF4J Server.
            - repository : The name of the target repository.
            - auth (optional): Tuple (username, password) for HTTP Basic Auth.
            - graph (optional): Named graph URI for scoped operations.

        Raises
        ------
        ValueError
            If the required configuration is missing.
        RuntimeError
            If the repository does not exist or the server is unreachable.
        """
        configuration = validate_config(config, required_keys=self.REQUIRED_KEYS, optional_defaults=self.OPTIONAL_DEFAULTS,
                                        alias_map=self.ALIASES, backend_name="RDF4J")

        super().__init__(configuration)
        self.base_url = configuration["base_url"].rstrip("/")
        self.repository = configuration["name"]
        self.auth = configuration["auth"]
        self.graph_uri = configuration["graph"]
        self.store_type = str(configuration["store_type"]).strip().lower()

        self.query_url = f"{self.base_url}/repositories/{self.repository}"
        self.update_url = f"{self.query_url}/statements"

        self.headers_query = {"Accept": "application/sparql-results+json"}
        self.headers_update = {"Content-Type": "application/sparql-update"}
        self.headers_load = {"Content-Type": "text/turtle"}

        self._ensure_repository_exists()

    def load(self, filename: str) -> None:
        """
        Load RDF triples from a Turtle (.ttl) file into the RDF4J repository.

        Parameters
        ----------
        filename : str
            Path to the Turtle (.ttl) file to be loaded.

        Raises
        ------
        FileNotFoundError
            If the file does not exist.
        RuntimeError
            If the server returns an error status during data loading.
        """
        if not Path(filename).exists():
            msg = f"[RDF4J] File not found: {filename}"
            raise FileNotFoundError(msg)

        rdf_data = Path(filename).read_bytes()
        params = {}
        if self.graph_uri:
            params["context"] = f"<{self.graph_uri}>"
        response = requests.post(self.update_url, headers=self.headers_load, params=params, data=rdf_data, auth=self.auth, timeout=None)

        if response.status_code not in {200, 204, 201}:
            msg = f"[RDF4J] Load failed with status {response.status_code}:\n{response.text}"
            raise RuntimeError(msg)

    def add(self, s: str, p: str, o: str) -> None:
        """
        Add a triple to the RDF4J store.

        Parameters
        ----------
        s : str
            The subject URI of the triple.
        p : str
            The predicate URI of the triple.
        o : str
            The object URI of the triple.
        """
        triple = f"<{s}> <{p}> <{o}> ."
        sparql = (
            f"INSERT DATA {{ GRAPH <{self.graph_uri}> {{ {triple} }} }}"
            if self.graph_uri
            else f"INSERT DATA {{ {triple} }}"
        )
        self._run_update(sparql)

    def delete(self, s: str, p: str, o: str) -> None:
        """
        Delete a triple from the RDF4J store.

        Parameters
        ----------
        s : str
            The subject URI of the triple to remove.
        p : str
            The predicate URI of the triple to remove.
        o : str
            The object URI of the triple to remove.
        """
        triple = f"<{s}> <{p}> <{o}> ."
        sparql = (
            f"DELETE DATA {{ GRAPH <{self.graph_uri}> {{ {triple} }} }}"
            if self.graph_uri
            else f"DELETE DATA {{ {triple} }}"
        )
        self._run_update(sparql)

    def query(self, sparql: str) -> list[dict[str, str]]:
        """
        Execute a SPARQL SELECT query against the RDF4J repository.

        Parameters
        ----------
        sparql : str
            The SPARQL query string.

        Returns
        -------
        list[dict[str, str]]
            The list of query result bindings.

        Raises
        ------
        RuntimeError
            If the query fails or the server returns an error response.
        """
        response = requests.post(self.query_url, headers=self.headers_query, data={"query": sparql}, auth=self.auth, timeout=None)

        if response.status_code != 200:
            msg = f"[RDF4J] SPARQL query failed: {response.status_code}\n{response.text}"
            raise RuntimeError(msg)

        data = response.json()
        bindings = data.get("results", {}).get("bindings", [])
        return [{k: v["value"] for k, v in row.items()} for row in bindings]

    def execute(self, sparql: str) -> Any:
        """
        Execute any SPARQL query (SELECT, ASK, CONSTRUCT, DESCRIBE, UPDATE).

        Parameters
        ----------
        sparql : str
            The SPARQL query or update string.

        Returns
        -------
        Any
            - list of dict for SELECT
            - bool for ASK
            - str (Turtle RDF) for CONSTRUCT/DESCRIBE
            - None for UPDATE operations

        Raises
        ------
        RuntimeError
            If the server responds with an error status.
        """
        lines = [line.strip() for line in sparql.strip().splitlines() if line.strip()]

        query_type = ""
        for line in lines:
            upper = line.upper()
            if upper.startswith(("PREFIX ", "BASE ")):
                continue
            query_type = line.split(None, 1)[0].upper()
            break

        # SELECT / ASK
        if query_type in {"SELECT", "ASK"}:
            response = requests.post(self.query_url, headers=self.headers_query, data={"query": sparql}, auth=self.auth, timeout=None)
            if response.status_code != 200:
                msg = f"[RDF4J] Query failed {response.status_code}:\n{response.text}"
                raise RuntimeError(msg)
            data = response.json()
            if query_type == "ASK":
                return bool(data.get("boolean", False))
            bindings = data.get("results", {}).get("bindings", [])
            return [{k: v["value"] for k, v in row.items()} for row in bindings]

        # CONSTRUCT / DESCRIBE
        if query_type in {"CONSTRUCT", "DESCRIBE"}:
            response = requests.post(self.query_url, headers={"Accept": "text/turtle"}, data={"query": sparql}, auth=self.auth, timeout=None)
            if response.status_code != 200:
                msg = f"[RDF4J] Query failed {response.status_code}:\n{response.text}"
                raise RuntimeError(msg)
            return response.text

        #  UPDATE operations (INSERT, DELETE, CLEAR, DROP, LOAD, CREATE, etc.)
        if query_type in {
            "WITH", "INSERT", "DELETE", "LOAD", "CLEAR", "CREATE", "DROP",
            "MOVE", "COPY", "ADD", "MODIFY"
        }:
            self._run_update(sparql)
            return None

        msg = f"[RDF4J] Unsupported SPARQL keyword: {query_type}"
        raise RuntimeError(msg)

    def clear(self) -> None:
        """
        Remove all data from the RDF4J repository (default and named graphs).
        """
        sparql = f"CLEAR GRAPH <{self.graph_uri}>" if self.graph_uri else "CLEAR DEFAULT"
        self._run_update(sparql)

    def _run_update(self, sparql: str) -> None:
        """
        Execute a SPARQL update operation.

        Parameters
        ----------
        sparql : str
            The SPARQL update string to be sent to the server.

        Raises
        ------
        RuntimeError
            If the update operation fails with a non-success status code.
        """
        response = requests.post(self.update_url, headers=self.headers_update, data=sparql, auth=self.auth, timeout=None)

        if response.status_code not in {200, 204, 201}:
            msg = f"[RDF4J] SPARQL update failed: {response.status_code}\n{response.text}"
            raise RuntimeError(msg)

    def _build_repository_config(self) -> str:
        """
        Build a Turtle (TTL) configuration for creating an RDF4J repository.
        The configuration depends on the selected `store_type`:
        - memory : in-memory store (non-persistent)
        - native : disk-based store (persistent)

        Returns
        -------
        str
            A Turtle-formatted configuration string suitable for the RDF4J repository creation endpoint.

        Raises
        ------
        ValueError
           If an unsupported store_type is provided.
        """
        if self.store_type == "memory":
            return f"""
            @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#>.
            @prefix rep: <http://www.openrdf.org/config/repository#>.
            @prefix sr: <http://www.openrdf.org/config/repository/sail#>.
            @prefix sail: <http://www.openrdf.org/config/sail#>.
            @prefix ms: <http://www.openrdf.org/config/sail/memory#>.

            [] a rep:Repository ;
            rep:repositoryID "{self.repository}" ;
            rdfs:label "{self.repository}" ;
            rep:repositoryImpl [
                rep:repositoryType "openrdf:SailRepository" ;
                sr:sailImpl [
                    sail:sailType "openrdf:MemoryStore"
                ]
            ] .
            """.strip()

        if self.store_type == "native":
            return f"""
            @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#>.
            @prefix rep: <http://www.openrdf.org/config/repository#>.
            @prefix sr: <http://www.openrdf.org/config/repository/sail#>.
            @prefix sail: <http://www.openrdf.org/config/sail#>.
            @prefix ns: <http://www.openrdf.org/config/sail/native#>.

            [] a rep:Repository ;
            rep:repositoryID "{self.repository}" ;
            rdfs:label "{self.repository}" ;
            rep:repositoryImpl [
                rep:repositoryType "openrdf:SailRepository" ;
                sr:sailImpl [
                    sail:sailType "openrdf:NativeStore" ;
                    ns:tripleIndexes "spoc,posc" ;
                    ns:forceSync true
                ]
            ] .
            """.strip()

        msg = (
            f"[RDF4J] Unsupported store_type '{self.store_type}'. "
            f"Supported values are: 'memory', 'native'."
        )
        raise ValueError(msg)

    def _repository_exists(self) -> bool:
        """
        Check whether the configured repository already exists in RDF4J Server.

        Returns
        -------
        bool
            True if the repository exists, False otherwise.

        Raises
        ------
        RuntimeError
            If the server cannot be reached or returns an unexpected error.
        """
        list_url = f"{self.base_url}/repositories"
        headers = {"Accept": "application/sparql-results+json, application/json"}

        try:
            response = requests.get(list_url, headers=headers, timeout=30, auth=self.auth)
        except requests.RequestException as e:
            msg = f"[RDF4J] Could not connect to RDF4J at {list_url}: {e}"
            raise RuntimeError(msg) from e

        if response.status_code in {401, 403}:
            msg = (
                f"[RDF4J] Access denied while listing repositories "
                f"(HTTP {response.status_code})."
            )
            raise RuntimeError(msg)

        if response.status_code != 200:
            msg = (
                f"[RDF4J] Failed to retrieve repository list: "
                f"{response.status_code} {response.text}"
            )
            raise RuntimeError(msg)

        content_type = response.headers.get("Content-Type", "").lower()

        if "json" in content_type:
            try:
                data = response.json()
            except ValueError as e:
                msg = "[RDF4J] Failed to parse repository list response as JSON."
                raise RuntimeError(msg) from e

            bindings = data.get("results", {}).get("bindings", [])
            repo_ids = {row.get("id", {}).get("value") for row in bindings if row.get("id", {}).get("value")}
            return self.repository in repo_ids

        text = response.text
        markers = (
            f">{self.repository}<",
            f'"{self.repository}"',
            f"'{self.repository}'",
            f"/repositories/{self.repository}",
        )
        return any(marker in text for marker in markers)

    def _ensure_repository_exists(self) -> None:
        """
        Ensure that the configured repository exists in the RDF4J server.
        If the repository does not exist, attempt to create it using the
        RDF4J REST API and a generated Turtle configuration.

        Raises
        ------
        RuntimeError
            If the server cannot be reached or if repository creation fails.
        """
        if self._repository_exists():
            return

        config_ttl = self._build_repository_config()
        create_url = f"{self.base_url}/repositories/{self.repository}"
        create_headers = {"Content-Type": "text/turtle"}

        try:
            create_response = requests.put(create_url, headers=create_headers, data=config_ttl.encode("utf-8"), auth=self.auth, timeout=300)
        except requests.RequestException as e:
            msg = f"[RDF4J] Failed to create repository '{self.repository}': {e}"
            raise RuntimeError(msg) from e

        if create_response.status_code not in {200, 201, 204}:
            msg = (
                f"[RDF4J] Repository creation failed for '{self.repository}': "
                f"{create_response.status_code}\n{create_response.text}"
            )
            raise RuntimeError(msg)
