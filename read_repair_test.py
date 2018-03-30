import os
import time
import pytest
import logging

from cassandra import ConsistencyLevel, WriteTimeout, ReadTimeout
from cassandra.query import SimpleStatement
from ccmlib.node import Node
from pytest import raises

from dtest import Tester, create_ks
from tools.assertions import assert_one
from tools.data import rows_to_list
from tools.jmxutils import JolokiaAgent, make_mbean
from tools.misc import retry_till_success

since = pytest.mark.since
logger = logging.getLogger(__name__)


class TestReadRepair(Tester):

    @pytest.fixture(scope='function', autouse=True)
    def fixture_set_cluster_settings(self, fixture_dtest_setup):
        cluster = fixture_dtest_setup.cluster
        cluster.populate(3)
        # disable dynamic snitch to make replica selection deterministic
        # when we use patient_exclusive_cql_connection, CL=1 and RF=n
        cluster.set_configuration_options(values={'hinted_handoff_enabled': False,
                                                  'endpoint_snitch': 'GossipingPropertyFileSnitch',
                                                  'dynamic_snitch': False})
        for node in cluster.nodelist():
            with open(os.path.join(node.get_conf_dir(), 'cassandra-rackdc.properties'), 'w') as snitch_file:
                snitch_file.write("dc=datacenter1" + os.linesep)
                snitch_file.write("rack=rack1" + os.linesep)
                snitch_file.write("prefer_local=true" + os.linesep)

        cluster.start(wait_for_binary_proto=True)

    @since('3.0')
    def test_alter_rf_and_run_read_repair(self):
        """
        @jira_ticket CASSANDRA-10655
        @jira_ticket CASSANDRA-10657

        Test that querying only a subset of all the columns in a row doesn't confuse read-repair to avoid
        the problem described in CASSANDRA-10655.
        """

        # session is only used to setup & do schema modification. Actual data queries are done directly on
        # each node, using an exclusive connection and CL.ONE
        session = self.patient_cql_connection(self.cluster.nodelist()[0])
        initial_replica, non_replicas = self.do_initial_setup(session)

        # Execute a query at CL.ALL on one of the nodes which was *not* the initial replica. It should trigger a
        # read repair and propagate the data to all 3 nodes.
        # Note: result of the read repair contains only the selected column (a), not all columns
        logger.debug("Executing 'SELECT a...' on non-initial replica to trigger read repair " + non_replicas[0].name)
        read_repair_session = self.patient_exclusive_cql_connection(non_replicas[0])
        assert_one(read_repair_session, "SELECT a FROM alter_rf_test.t1 WHERE k=1", [1], cl=ConsistencyLevel.ALL)

        # The read repair should have repaired the replicas, at least partially (see CASSANDRA-10655)
        # verify by querying each replica in turn.
        self.check_data_on_each_replica(expect_fully_repaired=False, initial_replica=initial_replica)

        # Now query again at CL.ALL but this time selecting all columns, which should ensure that 'b' also gets repaired
        query = "SELECT * FROM alter_rf_test.t1 WHERE k=1"
        logger.debug("Executing 'SELECT *...' on non-initial replica to trigger read repair " + non_replicas[0].name)
        assert_one(read_repair_session, query, [1, 1, 1], cl=ConsistencyLevel.ALL)

        # Check each replica individually again now that we expect the data to be fully repaired
        self.check_data_on_each_replica(expect_fully_repaired=True, initial_replica=initial_replica)

    def test_read_repair_chance(self):
        """
        @jira_ticket CASSANDRA-12368
        """
        # session is only used to setup & do schema modification. Actual data queries are done directly on
        # each node, using an exclusive connection and CL.ONE
        session = self.patient_cql_connection(self.cluster.nodelist()[0])
        initial_replica, non_replicas = self.do_initial_setup(session)

        # To ensure read repairs are triggered, set the table property to 100%
        logger.debug("Setting table read repair chance to 1")
        session.execute("""ALTER TABLE alter_rf_test.t1 WITH read_repair_chance = 1;""")

        # Execute a query at CL.ONE on one of the nodes which was *not* the initial replica. It should trigger a
        # read repair because read_repair_chance == 1, and propagate the data to all 3 nodes.
        # Note: result of the read repair contains only the selected column (a), not all columns, so we won't expect
        # 'b' to have been fully repaired afterwards.
        logger.debug("Executing 'SELECT a...' on non-initial replica to trigger read repair " + non_replicas[0].name)
        read_repair_session = self.patient_exclusive_cql_connection(non_replicas[0])
        read_repair_session.execute(SimpleStatement("SELECT a FROM alter_rf_test.t1 WHERE k=1",
                                                    consistency_level=ConsistencyLevel.ONE))

        # Query each replica individually to ensure that read repair was triggered. We should expect that only
        # the initial replica has data for both the 'a' and 'b' columns. The read repair should only have affected
        # the selected column, so the other two replicas should only have that data.
        # Note: we need to temporarily set read_repair_chance to 0 while we perform this check.
        logger.debug("Setting table read repair chance to 0 while we verify each replica's data")
        session.execute("""ALTER TABLE alter_rf_test.t1 WITH read_repair_chance = 0;""")
        # The read repair is run in the background, so we spin while checking that the repair has completed
        retry_till_success(self.check_data_on_each_replica,
                           expect_fully_repaired=False,
                           initial_replica=initial_replica,
                           timeout=30,
                           bypassed_exception=NotRepairedException)

        # Re-enable global read repair and perform another query on a non-replica. This time the query selects all
        # columns so we also expect the value for 'b' to be repaired.
        logger.debug("Setting table read repair chance to 1")
        session.execute("""ALTER TABLE alter_rf_test.t1 WITH read_repair_chance = 1;""")
        logger.debug("Executing 'SELECT *...' on non-initial replica to trigger read repair " + non_replicas[0].name)
        read_repair_session = self.patient_exclusive_cql_connection(non_replicas[0])
        read_repair_session.execute(SimpleStatement("SELECT * FROM alter_rf_test.t1 WHERE k=1",
                                                    consistency_level=ConsistencyLevel.ONE))

        # Query each replica again to ensure that second read repair was triggered. This time, we expect the
        # data to be fully repaired (both 'a' and 'b' columns) by virtue of the query being 'SELECT *...'
        # As before, we turn off read repair before doing this check.
        logger.debug("Setting table read repair chance to 0 while we verify each replica's data")
        session.execute("""ALTER TABLE alter_rf_test.t1 WITH read_repair_chance = 0;""")
        retry_till_success(self.check_data_on_each_replica,
                           expect_fully_repaired=True,
                           initial_replica=initial_replica,
                           timeout=30,
                           bypassed_exception=NotRepairedException)

    def do_initial_setup(self, session):
        """
        Create a keyspace with rf=1 and a table containing a single row with 2 non-primary key columns.
        Insert 1 row, placing the data on a single initial replica. Then, alter the keyspace to rf=3, but don't
        repair. Tests will execute various reads on the replicas and assert the effects of read repair.
        :param session: Used to perform the schema setup & insert the data
        :return: a tuple containing the node which initially acts as the replica, and a list of the other two nodes
        """
        # Disable speculative retry and [dclocal]read_repair in initial setup.
        session.execute("""CREATE KEYSPACE alter_rf_test
                           WITH replication = {'class': 'SimpleStrategy', 'replication_factor': 1};""")
        session.execute("""CREATE TABLE alter_rf_test.t1 (k int PRIMARY KEY, a int, b int)
                           WITH speculative_retry='NONE'
                           AND read_repair_chance=0
                           AND dclocal_read_repair_chance=0;""")
        session.execute("INSERT INTO alter_rf_test.t1 (k, a, b) VALUES (1, 1, 1);")

        # identify the initial replica and trigger a flush to ensure reads come from sstables
        initial_replica, non_replicas = self.identify_initial_placement()
        logger.debug("At RF=1 replica for data is " + initial_replica.name)
        initial_replica.flush()

        # Just some basic validation.
        # At RF=1, it shouldn't matter which node we query, as the actual data should always come from the
        # initial replica when reading at CL ONE
        for n in self.cluster.nodelist():
            logger.debug("Checking " + n.name)
            session = self.patient_exclusive_cql_connection(n)
            assert_one(session, "SELECT * FROM alter_rf_test.t1 WHERE k=1", [1, 1, 1], cl=ConsistencyLevel.ONE)

        # Alter so RF=n but don't repair, calling tests will execute queries to exercise read repair,
        # either at CL.ALL or after setting read_repair_chance to 100%.
        logger.debug("Changing RF from 1 to 3")
        session.execute("""ALTER KEYSPACE alter_rf_test
                           WITH replication = {'class': 'SimpleStrategy', 'replication_factor': 3};""")

        return initial_replica, non_replicas

    def identify_initial_placement(self):
        """
        Identify which node in the 3 node cluster contains the specific key at the point that the test keyspace has
        rf=1.
        :return: tuple containing the initial replica, plus a list of the other 2 replicas.
        """
        nodes = self.cluster.nodelist()
        out, _, _ = nodes[0].nodetool("getendpoints alter_rf_test t1 1")
        address = out.split('\n')[-2]
        initial_replica = None
        non_replicas = []
        for node in nodes:
            if node.address() == address:
                initial_replica = node
            else:
                non_replicas.append(node)

        assert initial_replica is not None, "Couldn't identify initial replica"

        return initial_replica, non_replicas

    def check_data_on_each_replica(self, expect_fully_repaired, initial_replica):
        """
        Perform a SELECT * query at CL.ONE on each replica in turn. If expect_fully_repaired is True, we verify that
        each replica returns the full row being queried. If not, then we only verify that the 'a' column has been
        repaired.
        """
        stmt = SimpleStatement("SELECT * FROM alter_rf_test.t1 WHERE k=1", consistency_level=ConsistencyLevel.ONE)
        logger.debug("Checking all if read repair has completed on all replicas")
        for n in self.cluster.nodelist():
            logger.debug("Checking {n}, {x}expecting all columns"
                         .format(n=n.name, x="" if expect_fully_repaired or n == initial_replica else "not "))
            session = self.patient_exclusive_cql_connection(n)
            res = rows_to_list(session.execute(stmt))
            logger.debug("Actual result: " + str(res))
            expected = [[1, 1, 1]] if expect_fully_repaired or n == initial_replica else [[1, 1, None]]
            if res != expected:
                raise NotRepairedException()

    @since('2.0')
    def test_range_slice_query_with_tombstones(self):
        """
        @jira_ticket CASSANDRA-8989
        @jira_ticket CASSANDRA-9502

        Range-slice queries with CL>ONE do unnecessary read-repairs.
        Reading from table which contains collection type using token function and with CL > ONE causes overwhelming writes to replicas.


        It's possible to check the behavior with tracing - pattern matching in system_traces.events.activity
        """
        node1 = self.cluster.nodelist()[0]
        session1 = self.patient_exclusive_cql_connection(node1)

        session1.execute("CREATE KEYSPACE ks WITH replication = {'class': 'NetworkTopologyStrategy', 'datacenter1': 2}")
        session1.execute("""
            CREATE TABLE ks.cf (
                key    int primary key,
                value  double,
                txt    text
            );
        """)

        for n in range(1, 2500):
            str = "foo bar %d iuhiu iuhiu ihi" % n
            session1.execute("INSERT INTO ks.cf (key, value, txt) VALUES (%d, %d, '%s')" % (n, n, str))

        self.cluster.flush()
        self.cluster.stop()
        self.cluster.start(wait_for_binary_proto=True)
        session1 = self.patient_exclusive_cql_connection(node1)

        for n in range(1, 1000):
            session1.execute("DELETE FROM ks.cf WHERE key = %d" % (n))

        time.sleep(1)

        node1.flush()

        time.sleep(1)

        query = SimpleStatement("SELECT * FROM ks.cf LIMIT 100", consistency_level=ConsistencyLevel.LOCAL_QUORUM)
        future = session1.execute_async(query, trace=True)
        future.result()
        trace = future.get_query_trace(max_wait=120)
        self.pprint_trace(trace)
        for trace_event in trace.events:
            # Step 1, find coordinator node:
            activity = trace_event.description
            assert "Appending to commitlog" not in activity
            assert "Adding to cf memtable" not in activity
            assert "Acquiring switchLock read lock" not in activity

    @since('3.0')
    def test_gcable_tombstone_resurrection_on_range_slice_query(self):
        """
        @jira_ticket CASSANDRA-11427

        Range queries before the 11427 will trigger read repairs for puregable tombstones on hosts that already compacted given tombstones.
        This will result in constant transfer and compaction actions sourced by few nodes seeding purgeable tombstones and triggered e.g.
        by periodical jobs scanning data range wise.
        """

        node1, node2, _ = self.cluster.nodelist()

        session1 = self.patient_cql_connection(node1)
        create_ks(session1, 'gcts', 3)
        query = """
            CREATE TABLE gcts.cf1 (
                key text,
                c1 text,
                PRIMARY KEY (key, c1)
            )
            WITH gc_grace_seconds=0
            AND compaction = {'class': 'SizeTieredCompactionStrategy', 'enabled': 'false'};
        """
        session1.execute(query)

        # create row tombstone
        delete_stmt = SimpleStatement("DELETE FROM gcts.cf1 WHERE key = 'a'", consistency_level=ConsistencyLevel.ALL)
        session1.execute(delete_stmt)

        # flush single sstable with tombstone
        node1.flush()
        node2.flush()

        # purge tombstones from node2 (gc grace 0)
        node2.compact()

        # execute range slice query, which should not trigger read-repair for purged TS
        future = session1.execute_async(SimpleStatement("SELECT * FROM gcts.cf1", consistency_level=ConsistencyLevel.ALL), trace=True)
        future.result()
        trace = future.get_query_trace(max_wait=120)
        self.pprint_trace(trace)
        for trace_event in trace.events:
            activity = trace_event.description
            assert "Sending READ_REPAIR message" not in activity

    def pprint_trace(self, trace):
        """Pretty print a trace"""
        if logging.root.level == logging.DEBUG:
            print(("-" * 40))
            for t in trace.events:
                print(("%s\t%s\t%s\t%s" % (t.source, t.source_elapsed, t.description, t.thread_name)))
            print(("-" * 40))


def quorum(query_string):
    return SimpleStatement(query_string=query_string, consistency_level=ConsistencyLevel.QUORUM)


kcv = lambda k, c, v: [k, c, v]


listify = lambda results: [list(r) for r in results]


class StorageProxy(object):

    def __init__(self, node):
        assert isinstance(node, Node)
        self.node = node
        self.jmx = JolokiaAgent(node)

    def start(self):
        self.jmx.start()

    def stop(self):
        self.jmx.stop()

    def _get_metric(self, metric):
        mbean = make_mbean("metrics", type="ReadRepair", name=metric)
        return self.jmx.read_attribute(mbean, "Count")

    @property
    def blocking_read_repair(self):
        return self._get_metric("RepairedBlocking")

    @property
    def speculated_data_request(self):
        return self._get_metric("SpeculatedData")

    @property
    def speculated_data_repair(self):
        return self._get_metric("SpeculatedRepair")

    def get_table_metric(self, keyspace, table, metric, attr="Count"):
        mbean = make_mbean("metrics", keyspace=keyspace, scope=table, type="Table", name=metric)
        return self.jmx.read_attribute(mbean, attr)

    def __enter__(self):
        """ For contextmanager-style usage. """
        self.start()
        return self

    def __exit__(self, exc_type, value, traceback):
        """ For contextmanager-style usage. """
        self.stop()


class TestSpeculativeReadRepair(Tester):

    @pytest.fixture(scope='function', autouse=True)
    def fixture_set_cluster_settings(self, fixture_dtest_setup):
        cluster = fixture_dtest_setup.cluster
        cluster.set_configuration_options(values={'hinted_handoff_enabled': False,
                                                  'dynamic_snitch': False,
                                                  'write_request_timeout_in_ms': 500,
                                                  'read_request_timeout_in_ms': 500})
        cluster.populate(3, install_byteman=True, debug=True).start(wait_for_binary_proto=True,
                                                                    jvm_args=['-XX:-PerfDisableSharedMem'])
        session = fixture_dtest_setup.patient_exclusive_cql_connection(cluster.nodelist()[0], timeout=2)

        session.execute("CREATE KEYSPACE ks WITH replication = {'class': 'SimpleStrategy', 'replication_factor': 3}")
        session.execute("CREATE TABLE ks.tbl (k int, c int, v int, primary key (k, c)) "
                        "WITH speculative_retry = '100ms' "
                        "AND read_repair_chance = 0.0 "
                        "AND dclocal_read_repair_chance = 0.0;")

    def get_cql_connection(self, node, **kwargs):
        return self.patient_exclusive_cql_connection(node, retry_policy=None, **kwargs)


    def test_failed_read_repair(self):
        """
        If none of the disagreeing nodes ack the repair mutation, the read should fail
        """
        node1, node2, node3 = self.cluster.nodelist()
        assert isinstance(node1, Node)
        assert isinstance(node2, Node)
        assert isinstance(node3, Node)

        session = self.get_cql_connection(node1, timeout=2)
        session.execute(quorum("INSERT INTO ks.tbl (k, c, v) VALUES (1, 0, 1)"))

        node2.byteman_submit(['./byteman/read_repair/stop_writes.btm'])
        node3.byteman_submit(['./byteman/read_repair/stop_writes.btm'])
        node2.byteman_submit(['./byteman/read_repair/stop_rr_writes.btm'])
        node3.byteman_submit(['./byteman/read_repair/stop_rr_writes.btm'])

        with raises(WriteTimeout):
            session.execute(quorum("INSERT INTO ks.tbl (k, c, v) VALUES (1, 1, 2)"))

        node2.byteman_submit(['./byteman/read_repair/sorted_live_endpoints.btm'])
        session = self.get_cql_connection(node2)
        with StorageProxy(node2) as storage_proxy:
            assert storage_proxy.blocking_read_repair == 0
            assert storage_proxy.speculated_data_request == 0
            assert storage_proxy.speculated_data_repair == 0

            with raises(ReadTimeout):
                session.execute(quorum("SELECT * FROM ks.tbl WHERE k=1"))

            assert storage_proxy.blocking_read_repair > 0
            assert storage_proxy.speculated_data_request == 0
            assert storage_proxy.speculated_data_repair > 0

    def test_normal_read_repair(self):
        """
        
        :return: 
        """
        node1, node2, node3 = self.cluster.nodelist()
        assert isinstance(node1, Node)
        assert isinstance(node2, Node)
        assert isinstance(node3, Node)
        session = self.get_cql_connection(node1, timeout=2)

        session.execute(quorum("INSERT INTO ks.tbl (k, c, v) VALUES (1, 0, 1)"))

        node2.byteman_submit(['./byteman/read_repair/stop_writes.btm'])
        node3.byteman_submit(['./byteman/read_repair/stop_writes.btm'])

        session.execute("INSERT INTO ks.tbl (k, c, v) VALUES (1, 1, 2)")

        # re-enable writes
        node2.byteman_submit(['-u', './byteman/read_repair/stop_writes.btm'])

        node2.byteman_submit(['./byteman/read_repair/sorted_live_endpoints.btm'])
        with StorageProxy(node2) as storage_proxy:
            assert storage_proxy.blocking_read_repair == 0
            assert storage_proxy.speculated_data_request == 0
            assert storage_proxy.speculated_data_repair == 0

            session = self.get_cql_connection(node2)
            expected = [kcv(1, 0, 1), kcv(1, 1, 2)]
            results = session.execute(quorum("SELECT * FROM ks.tbl WHERE k=1"))
            assert listify(results) == expected

            assert storage_proxy.blocking_read_repair == 1
            assert storage_proxy.speculated_data_request == 0
            assert storage_proxy.speculated_data_repair == 0

    def test_speculative_data_request(self):
        """ If one node doesn't respond to a full data request, it should query the other """
        node1, node2, node3 = self.cluster.nodelist()
        assert isinstance(node1, Node)
        assert isinstance(node2, Node)
        assert isinstance(node3, Node)
        session = self.get_cql_connection(node1, timeout=2)

        session.execute(quorum("INSERT INTO ks.tbl (k, c, v) VALUES (1, 0, 1)"))

        node2.byteman_submit(['./byteman/read_repair/stop_writes.btm'])
        node3.byteman_submit(['./byteman/read_repair/stop_writes.btm'])

        session.execute("INSERT INTO ks.tbl (k, c, v) VALUES (1, 1, 2)")

        # re-enable writes
        node2.byteman_submit(['-u', './byteman/read_repair/stop_writes.btm'])

        node1.byteman_submit(['./byteman/read_repair/sorted_live_endpoints.btm'])
        with StorageProxy(node1) as storage_proxy:
            assert storage_proxy.blocking_read_repair == 0
            assert storage_proxy.speculated_data_request == 0
            assert storage_proxy.speculated_data_repair == 0

            session = self.get_cql_connection(node1)
            node2.byteman_submit(['./byteman/read_repair/stop_data_reads.btm'])
            results = session.execute(quorum("SELECT * FROM ks.tbl WHERE k=1"))
            assert listify(results) == [kcv(1, 0, 1), kcv(1, 1, 2)]

            assert storage_proxy.blocking_read_repair == 1
            assert storage_proxy.speculated_data_request == 1
            assert storage_proxy.speculated_data_repair == 0

    def test_speculative_write(self):
        """ if one node doesn't respond to a read repair mutation, it should be sent to the remaining node """
        """
        
        :return: 
        """
        node1, node2, node3 = self.cluster.nodelist()
        assert isinstance(node1, Node)
        assert isinstance(node2, Node)
        assert isinstance(node3, Node)
        session = self.get_cql_connection(node1, timeout=2)

        session.execute(quorum("INSERT INTO ks.tbl (k, c, v) VALUES (1, 0, 1)"))

        node2.byteman_submit(['./byteman/read_repair/stop_writes.btm'])
        node3.byteman_submit(['./byteman/read_repair/stop_writes.btm'])

        session.execute("INSERT INTO ks.tbl (k, c, v) VALUES (1, 1, 2)")

        # re-enable writes on node 3, leave them off on node2
        node2.byteman_submit(['./byteman/read_repair/stop_rr_writes.btm'])

        node1.byteman_submit(['./byteman/read_repair/sorted_live_endpoints.btm'])
        with StorageProxy(node1) as storage_proxy:
            assert storage_proxy.blocking_read_repair == 0
            assert storage_proxy.speculated_data_request == 0
            assert storage_proxy.speculated_data_repair == 0

            session = self.get_cql_connection(node1)
            expected = [kcv(1, 0, 1), kcv(1, 1, 2)]
            results = session.execute(quorum("SELECT * FROM ks.tbl WHERE k=1"))
            assert listify(results) == expected

            assert storage_proxy.blocking_read_repair == 1
            assert storage_proxy.speculated_data_request == 0
            assert storage_proxy.speculated_data_repair == 1

    def test_quorum_requirement(self):
        """
        Even if we speculate on every stage, we should still only require a quorum of responses for success
        """
        node1, node2, node3 = self.cluster.nodelist()
        assert isinstance(node1, Node)
        assert isinstance(node2, Node)
        assert isinstance(node3, Node)
        session = self.get_cql_connection(node1, timeout=2)

        session.execute(quorum("INSERT INTO ks.tbl (k, c, v) VALUES (1, 0, 1)"))

        node2.byteman_submit(['./byteman/read_repair/stop_writes.btm'])
        node3.byteman_submit(['./byteman/read_repair/stop_writes.btm'])

        session.execute("INSERT INTO ks.tbl (k, c, v) VALUES (1, 1, 2)")

        # re-enable writes
        node2.byteman_submit(['-u', './byteman/read_repair/stop_writes.btm'])
        node3.byteman_submit(['-u', './byteman/read_repair/stop_writes.btm'])

        # force endpoint order
        node1.byteman_submit(['./byteman/read_repair/sorted_live_endpoints.btm'])

        # node2.byteman_submit(['./byteman/read_repair/stop_digest_reads.btm'])
        node2.byteman_submit(['./byteman/read_repair/stop_data_reads.btm'])
        node3.byteman_submit(['./byteman/read_repair/stop_rr_writes.btm'])

        with StorageProxy(node1) as storage_proxy:
            assert storage_proxy.get_table_metric("ks", "tbl", "SpeculativeRetries") == 0
            assert storage_proxy.blocking_read_repair == 0
            assert storage_proxy.speculated_data_request == 0
            assert storage_proxy.speculated_data_repair == 0

            session = self.get_cql_connection(node1)
            expected = [kcv(1, 0, 1), kcv(1, 1, 2)]
            results = session.execute(quorum("SELECT * FROM ks.tbl WHERE k=1"))
            assert listify(results) == expected

            assert storage_proxy.get_table_metric("ks", "tbl", "SpeculativeRetries") == 0
            assert storage_proxy.blocking_read_repair == 1
            assert storage_proxy.speculated_data_request == 1
            assert storage_proxy.speculated_data_repair == 1

    def test_quorum_requirement_on_speculated_read(self):
        """
        Even if we speculate on every stage, we should still only require a quorum of responses for success
        """
        node1, node2, node3 = self.cluster.nodelist()
        assert isinstance(node1, Node)
        assert isinstance(node2, Node)
        assert isinstance(node3, Node)
        session = self.get_cql_connection(node1, timeout=2)

        session.execute(quorum("INSERT INTO ks.tbl (k, c, v) VALUES (1, 0, 1)"))

        node2.byteman_submit(['./byteman/read_repair/stop_writes.btm'])
        node3.byteman_submit(['./byteman/read_repair/stop_writes.btm'])

        session.execute("INSERT INTO ks.tbl (k, c, v) VALUES (1, 1, 2)")

        # re-enable writes
        node2.byteman_submit(['-u', './byteman/read_repair/stop_writes.btm'])
        node3.byteman_submit(['-u', './byteman/read_repair/stop_writes.btm'])

        # force endpoint order
        node1.byteman_submit(['./byteman/read_repair/sorted_live_endpoints.btm'])

        node2.byteman_submit(['./byteman/read_repair/stop_digest_reads.btm'])
        node3.byteman_submit(['./byteman/read_repair/stop_data_reads.btm'])
        node2.byteman_submit(['./byteman/read_repair/stop_rr_writes.btm'])

        with StorageProxy(node1) as storage_proxy:
            assert storage_proxy.get_table_metric("ks", "tbl", "SpeculativeRetries") == 0
            assert storage_proxy.blocking_read_repair == 0
            assert storage_proxy.speculated_data_request == 0
            assert storage_proxy.speculated_data_repair == 0

            session = self.get_cql_connection(node1)
            expected = [kcv(1, 0, 1), kcv(1, 1, 2)]
            # import pdb; pdb.set_trace()
            results = session.execute(quorum("SELECT * FROM ks.tbl WHERE k=1"))
            assert listify(results) == expected

            assert storage_proxy.get_table_metric("ks", "tbl", "SpeculativeRetries") == 1
            assert storage_proxy.blocking_read_repair == 1
            assert storage_proxy.speculated_data_request == 0  # we'll ask everyone we sent the initial read to
            assert storage_proxy.speculated_data_repair == 1


class NotRepairedException(Exception):
    """
    Thrown to indicate that the data on a replica hasn't been doesn't match what we'd expect if a
    specific read repair has run. See check_data_on_each_replica.
    """
    pass

