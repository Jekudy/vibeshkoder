"""GraphAdapter Protocol + two implementations (T10-01).

Two implementations:
- Neo4jAdapter  — production, async-await over bolt in prod. Reads NEO4J_* from settings.
- NetworkXAdapter — in-memory test fake (marked @pytest.mark.graph_unit). No persistence.

Cypher injection safety:
  ALL admin input is bound as query parameters.
  max_hops is hardcoded in the Cypher template (NOT admin-controllable).
  max_results is bound as a parameter.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

import networkx as nx

logger = logging.getLogger(__name__)

# ─── Protocol ────────────────────────────────────────────────────────────────


@runtime_checkable
class GraphAdapter(Protocol):
    """Adapter protocol for Phase 10 graph projection.

    Two implementations:
    - Neo4jAdapter (production, async-await over bolt+s in prod)
    - NetworkXAdapter (unit-test in-memory fake; marked @pytest.mark.graph_unit)
    """

    async def merge_node(
        self,
        node_key: str,
        labels: list[str],
        properties: dict,
    ) -> None: ...

    async def merge_edge(
        self,
        edge_key: str,
        source_key: str,
        target_key: str,
        relationship_type: str,
        properties: dict,
    ) -> None: ...

    async def delete_provenance(self, provenance_id: str) -> int:
        """Delete edges/nodes derived from provenance_id. Returns purged count."""
        ...

    async def query_traversal(
        self,
        topic: str,
        max_hops: int,
        max_results: int,
    ) -> list[dict]: ...

    async def query_traversal_with_paths(
        self,
        start_label: str,
        end_label: str,
        max_hops: int,
        max_results: int,
    ) -> list[dict]: ...

    async def health_check(self) -> bool: ...

    async def close(self) -> None: ...


# ─── Neo4jAdapter — production ────────────────────────────────────────────────


class Neo4jAdapter:
    """Production Neo4j adapter using the async bolt driver.

    Connection pool: max_connection_pool_size=10, connection_timeout=5,
    max_connection_lifetime=3600.

    All Cypher queries use bound parameters — NO string interpolation of any admin input.
    max_hops is hardcoded per template; max_results is bound.
    """

    def __init__(self) -> None:
        from bot.config import settings

        # Import lazily so the module can be imported without neo4j installed in test envs
        # that only use NetworkXAdapter.
        from neo4j import AsyncGraphDatabase  # type: ignore[import]

        self._driver = AsyncGraphDatabase.driver(
            settings.NEO4J_BOLT_URI,
            auth=(settings.NEO4J_AUTH_USER, settings.NEO4J_AUTH_PASSWORD),
            max_connection_pool_size=10,
            connection_timeout=5,
            max_connection_lifetime=3600,
        )
        self._database = settings.NEO4J_DATABASE

    async def merge_node(
        self,
        node_key: str,
        labels: list[str],
        properties: dict,
    ) -> None:
        """MERGE a node by node_key; accumulate provenance_ids on match."""
        provenance_id = properties.get("provenance_id")
        query = (
            "MERGE (n:MemoryNode {node_key: $node_key})\n"
            "ON CREATE SET\n"
            "    n += $properties,\n"
            "    n.provenance_ids = CASE WHEN $provenance_id IS NOT NULL"
            " THEN [$provenance_id] ELSE [] END,\n"
            "    n.created_at = datetime()\n"
            "ON MATCH SET\n"
            "    n += $properties,\n"
            "    n.provenance_ids = CASE WHEN $provenance_id IS NOT NULL\n"
            "        THEN n.provenance_ids + [$provenance_id]\n"
            "        ELSE n.provenance_ids END,\n"
            "    n.updated_at = datetime()\n"
            "RETURN n.node_key"
        )
        async with self._driver.session(database=self._database) as session:
            await session.run(
                query,
                node_key=node_key,
                properties=properties,
                provenance_id=provenance_id,
            )

    async def merge_edge(
        self,
        edge_key: str,
        source_key: str,
        target_key: str,
        relationship_type: str,
        properties: dict,
    ) -> None:
        """MERGE an edge between two existing MemoryNode nodes."""
        provenance_id = properties.get("provenance_id")
        query = (
            "MATCH (s:MemoryNode {node_key: $source_key})\n"
            "MATCH (o:MemoryNode {node_key: $target_key})\n"
            "MERGE (s)-[r:GRAPH_EDGE {edge_key: $edge_key}]->(o)\n"
            "ON CREATE SET\n"
            "    r.predicate = $relationship_type,\n"
            "    r.provenance_ids = CASE WHEN $provenance_id IS NOT NULL"
            " THEN [$provenance_id] ELSE [] END,\n"
            "    r.created_at = datetime()\n"
            "ON MATCH SET\n"
            "    r.provenance_ids = CASE WHEN $provenance_id IS NOT NULL\n"
            "        THEN r.provenance_ids + [$provenance_id]\n"
            "        ELSE r.provenance_ids END,\n"
            "    r.updated_at = datetime()\n"
            "RETURN r.edge_key"
        )
        async with self._driver.session(database=self._database) as session:
            await session.run(
                query,
                edge_key=edge_key,
                source_key=source_key,
                target_key=target_key,
                relationship_type=relationship_type,
                provenance_id=provenance_id,
            )

    async def delete_provenance(self, provenance_id: str) -> int:
        """Delete edges where provenance_id is in r.provenance_ids; orphan-cleanup nodes.

        Returns count of affected relationships.
        """
        # Step 1: detach provenance from relationships
        detach_query = (
            "MATCH ()-[r:GRAPH_EDGE]->()\n"
            "WHERE $provenance_id IN r.provenance_ids\n"
            "SET r.provenance_ids = [x IN r.provenance_ids WHERE x <> $provenance_id]\n"
            "WITH r\n"
            "WHERE size(r.provenance_ids) = 0\n"
            "DELETE r\n"
            "RETURN count(r) AS purged"
        )
        # Step 2: remove orphan nodes (no remaining provenance)
        orphan_query = (
            "MATCH (n:MemoryNode)\n"
            "WHERE size(n.provenance_ids) = 0\n"
            "DETACH DELETE n\n"
            "RETURN count(n) AS removed"
        )
        async with self._driver.session(database=self._database) as session:
            result = await session.run(detach_query, provenance_id=provenance_id)
            record = await result.single()
            purged = record["purged"] if record else 0
            await session.run(orphan_query)
        return purged

    async def query_traversal(
        self,
        topic: str,
        max_hops: int,
        max_results: int,
    ) -> list[dict]:
        """Parameterized traversal from a topic node.

        max_hops is constrained to [1, 5] and baked into the Cypher template string —
        it is NOT passed as a parameter (Cypher does not support variable hop counts via
        params). max_results is bound as a parameter.
        """
        # Clamp max_hops to safe range — template substitution only, not admin-user input
        safe_hops = max(1, min(max_hops, 5))
        query = (
            f"MATCH path = (start:MemoryNode)-[*1..{safe_hops}]-(end:MemoryNode)\n"
            "WHERE start.label = $topic\n"
            "WITH DISTINCT end\n"
            "RETURN end.label AS label, end.node_type AS node_type,"
            " end.node_key AS node_key\n"
            "LIMIT $max_results"
        )
        async with self._driver.session(database=self._database) as session:
            result = await session.run(query, topic=topic, max_results=max_results)
            records = await result.data()
        return [dict(r) for r in records]

    async def query_traversal_with_paths(
        self,
        start_label: str,
        end_label: str,
        max_hops: int,
        max_results: int,
    ) -> list[dict]:
        """Return paths (nodes + edges) from start_label to end_label using shortestPath.

        Each result dict has keys: nodes (list of {label, node_type, node_key}),
        edges (list of {source_key, target_key, predicate}).
        Bound as parameters — no string interpolation of user input.
        """
        safe_hops = max(1, min(max_hops, 5))
        query = (
            f"MATCH p = shortestPath((a:MemoryNode)-[*1..{safe_hops}]-(b:MemoryNode))\n"
            "WHERE a.label = $start_label AND b.label = $end_label\n"
            "RETURN "
            "[n IN nodes(p) | {label: n.label, node_type: n.node_type, node_key: n.node_key}] AS path_nodes,\n"
            "[r IN relationships(p) | {source_key: startNode(r).node_key, target_key: endNode(r).node_key, predicate: r.predicate}] AS path_edges\n"
            "LIMIT $max_results"
        )
        async with self._driver.session(database=self._database) as session:
            result = await session.run(
                query,
                start_label=start_label,
                end_label=end_label,
                max_results=max_results,
            )
            records = await result.data()
        return [{"nodes": r["path_nodes"], "edges": r["path_edges"]} for r in records]

    async def health_check(self) -> bool:
        """Run RETURN 1 to verify bolt connectivity."""
        try:
            async with self._driver.session(database=self._database) as session:
                result = await session.run("RETURN 1 AS ok")
                record = await result.single()
                return record is not None and record["ok"] == 1
        except Exception:
            logger.exception("neo4j health_check failed")
            return False

    async def close(self) -> None:
        await self._driver.close()


# ─── NetworkXAdapter — in-memory test fake ────────────────────────────────────


class NetworkXAdapter:
    """In-memory graph adapter backed by networkx.MultiDiGraph.

    For unit tests only — no persistence. Satisfies the GraphAdapter Protocol.
    """

    def __init__(self) -> None:
        self._graph: nx.MultiDiGraph = nx.MultiDiGraph()

    async def merge_node(
        self,
        node_key: str,
        labels: list[str],
        properties: dict,
    ) -> None:
        if self._graph.has_node(node_key):
            self._graph.nodes[node_key].update(properties)
            existing = self._graph.nodes[node_key].get("provenance_ids", [])
            pid = properties.get("provenance_id")
            if pid and pid not in existing:
                existing.append(pid)
                self._graph.nodes[node_key]["provenance_ids"] = existing
        else:
            node_data: dict[str, Any] = {**properties, "labels": labels}
            pid = properties.get("provenance_id")
            node_data["provenance_ids"] = [pid] if pid else []
            self._graph.add_node(node_key, **node_data)

    async def merge_edge(
        self,
        edge_key: str,
        source_key: str,
        target_key: str,
        relationship_type: str,
        properties: dict,
    ) -> None:
        # Find existing edge with matching edge_key
        for _u, _v, key, data in self._graph.edges(data=True, keys=True):  # type: ignore[misc]
            if data.get("edge_key") == edge_key:
                pid = properties.get("provenance_id")
                if pid:
                    existing_pids = data.get("provenance_ids", [])
                    if pid not in existing_pids:
                        existing_pids.append(pid)
                    data["provenance_ids"] = existing_pids
                return
        # New edge
        edge_data: dict[str, Any] = {
            **properties,
            "edge_key": edge_key,
            "relationship_type": relationship_type,
        }
        pid = properties.get("provenance_id")
        edge_data["provenance_ids"] = [pid] if pid else []
        self._graph.add_edge(source_key, target_key, **edge_data)

    async def delete_provenance(self, provenance_id: str) -> int:
        """Remove edges tagged with provenance_id; clean up orphan nodes.

        Returns count of edges removed.
        """
        edges_to_remove: list[tuple] = []
        for u, v, key, data in self._graph.edges(data=True, keys=True):  # type: ignore[misc]
            pids = data.get("provenance_ids", [])
            if provenance_id in pids:
                edges_to_remove.append((u, v, key))

        for u, v, key in edges_to_remove:
            self._graph.remove_edge(u, v, key=key)

        # Remove orphan nodes (degree 0, no remaining edges)
        orphans = [n for n in list(self._graph.nodes()) if self._graph.degree(n) == 0]
        # But only remove if no provenance left either
        for node in orphans:
            node_pids = self._graph.nodes[node].get("provenance_ids", [])
            if not node_pids:
                self._graph.remove_node(node)

        return len(edges_to_remove)

    async def query_traversal(
        self,
        topic: str,
        max_hops: int,
        max_results: int,
    ) -> list[dict]:
        """BFS traversal from the node whose 'label' matches topic."""
        safe_hops = max(1, min(max_hops, 5))

        # Find the start node by label
        start_nodes = [
            n for n, d in self._graph.nodes(data=True) if d.get("label") == topic
        ]
        if not start_nodes:
            return []

        results: list[dict] = []
        visited: set[str] = set()

        for start in start_nodes:
            # Include the start node itself
            start_data = dict(self._graph.nodes[start])
            if start not in visited:
                visited.add(start)
                results.append(
                    {
                        "label": start_data.get("label"),
                        "node_type": start_data.get("node_type"),
                        "node_key": start,
                    }
                )

            # BFS up to safe_hops
            frontier = {start}
            for _hop in range(safe_hops):
                next_frontier: set[str] = set()
                for node in frontier:
                    neighbors: set[str] = set()
                    neighbors.update(self._graph.successors(node))
                    neighbors.update(self._graph.predecessors(node))
                    for neighbor in neighbors:
                        if neighbor not in visited:
                            visited.add(neighbor)
                            nd = dict(self._graph.nodes[neighbor])
                            results.append(
                                {
                                    "label": nd.get("label"),
                                    "node_type": nd.get("node_type"),
                                    "node_key": neighbor,
                                }
                            )
                            next_frontier.add(neighbor)
                            if len(results) >= max_results:
                                return results[:max_results]
                frontier = next_frontier
                if not frontier:
                    break

        return results[:max_results]

    async def query_traversal_with_paths(
        self,
        start_label: str,
        end_label: str,
        max_hops: int,
        max_results: int,
    ) -> list[dict]:
        """Return shortest paths (nodes + edges) from start_label to end_label using BFS.

        Each result dict: {nodes: [{label, node_type, node_key}, ...], edges: [{source_key, target_key, predicate}, ...]}.
        """
        safe_hops = max(1, min(max_hops, 5))

        # Find start and end nodes by label
        start_nodes = [n for n, d in self._graph.nodes(data=True) if d.get("label") == start_label]
        end_nodes = [n for n, d in self._graph.nodes(data=True) if d.get("label") == end_label]

        if not start_nodes or not end_nodes:
            return []

        # Use networkx to find shortest paths between any start/end pair
        # Convert to undirected for traversal (symmetric reachability)
        undirected = self._graph.to_undirected()

        results: list[dict] = []
        for start in start_nodes:
            for end in end_nodes:
                if start == end:
                    continue
                try:
                    path_nodes_keys = nx.shortest_path(undirected, start, end)
                except nx.NetworkXNoPath:
                    continue

                if len(path_nodes_keys) - 1 > safe_hops:
                    continue

                # Build node dicts
                path_node_dicts = []
                for nk in path_nodes_keys:
                    nd = dict(self._graph.nodes[nk])
                    path_node_dicts.append({
                        "label": nd.get("label"),
                        "node_type": nd.get("node_type"),
                        "node_key": nk,
                    })

                # Build edge dicts for each consecutive pair
                path_edge_dicts = []
                for i in range(len(path_nodes_keys) - 1):
                    src = path_nodes_keys[i]
                    tgt = path_nodes_keys[i + 1]
                    # Find edge between src and tgt (either direction in original graph)
                    edge_data = {}
                    if self._graph.has_edge(src, tgt):
                        # Get first edge data
                        for key, data in self._graph[src][tgt].items():
                            edge_data = data
                            break
                    elif self._graph.has_edge(tgt, src):
                        for key, data in self._graph[tgt][src].items():
                            edge_data = data
                            break
                    path_edge_dicts.append({
                        "source_key": src,
                        "target_key": tgt,
                        "predicate": edge_data.get("predicate") or edge_data.get("relationship_type"),
                    })

                results.append({"nodes": path_node_dicts, "edges": path_edge_dicts})
                if len(results) >= max_results:
                    return results

        return results

    async def health_check(self) -> bool:
        return True

    async def close(self) -> None:
        pass

    @property
    def nodes(self) -> dict:
        """Return node dictionary (for test inspection)."""
        return dict(self._graph.nodes(data=True))
