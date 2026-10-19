"""Business-logic tests (no web framework needed): python -m unittest discover -s tests"""
import os
import sys
import tempfile
import threading
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import services  # noqa: E402
from app.db import connect, init_db  # noqa: E402
from app.errors import AppError  # noqa: E402


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "test.db")
        init_db(self.path)
        self.conn = connect(self.path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def product(self, sku="SKU-1", price="10.00", stock=10):
        return services.create_product(self.conn, sku, f"Item {sku}", Decimal(price), stock)

    # ---- products ----
    def test_create_and_list_products(self):
        p = self.product(price="19.99", stock=5)
        self.assertEqual(p["price"], "19.99")
        self.assertEqual(p["stock"], 5)
        self.product("SKU-2")
        result = services.list_products(self.conn, limit=10, offset=0)
        self.assertEqual(result["total"], 2)
        self.assertEqual([i["sku"] for i in result["items"]], ["SKU-1", "SKU-2"])

    def test_duplicate_sku_is_conflict_case_insensitive(self):
        self.product("ABC")
        with self.assertRaises(AppError) as cm:
            self.product("abc")
        self.assertEqual((cm.exception.status_code, cm.exception.code), (409, "DUPLICATE_SKU"))

    def test_get_missing_product_is_404(self):
        with self.assertRaises(AppError) as cm:
            services.get_product(self.conn, 999)
        self.assertEqual(cm.exception.status_code, 404)

    # ---- stock ----
    def test_stock_set_and_adjust(self):
        p = self.product(stock=10)
        r = services.update_stock(self.conn, p["id"], adjust_by=-4)
        self.assertEqual((r["previous_stock"], r["stock"]), (10, 6))
        r = services.update_stock(self.conn, p["id"], set_to=50)
        self.assertEqual((r["previous_stock"], r["stock"]), (6, 50))
        self.assertEqual(services.get_stock(self.conn, p["id"])["stock"], 50)

    def test_stock_cannot_go_negative(self):
        p = self.product(stock=3)
        with self.assertRaises(AppError) as cm:
            services.update_stock(self.conn, p["id"], adjust_by=-4)
        self.assertEqual((cm.exception.status_code, cm.exception.code), (409, "INSUFFICIENT_STOCK"))
        self.assertEqual(services.get_stock(self.conn, p["id"])["stock"], 3)

    def test_stock_update_unknown_product(self):
        with self.assertRaises(AppError) as cm:
            services.update_stock(self.conn, 42, set_to=1)
        self.assertEqual(cm.exception.status_code, 404)

    # ---- orders ----
    def test_order_totals_computed_and_stock_deducted(self):
        a = self.product("A", "19.99", 10)
        b = self.product("B", "0.10", 10)
        order = services.create_order(self.conn, [(a["id"], 3), (b["id"], 3)])
        self.assertEqual(order["status"], "confirmed")
        self.assertEqual([l["line_total"] for l in order["items"]], ["59.97", "0.30"])
        self.assertEqual(order["total"], "60.27")  # exact: no float drift
        self.assertEqual(services.get_stock(self.conn, a["id"])["stock"], 7)
        self.assertEqual(services.get_stock(self.conn, b["id"])["stock"], 7)
        self.assertEqual(services.get_order(self.conn, order["id"])["total"], "60.27")

    def test_duplicate_lines_are_merged(self):
        a = self.product("A", "5.00", 10)
        order = services.create_order(self.conn, [(a["id"], 2), (a["id"], 3)])
        self.assertEqual(len(order["items"]), 1)
        self.assertEqual(order["items"][0]["quantity"], 5)
        self.assertEqual(order["total"], "25.00")

    def test_order_with_unknown_product_changes_nothing(self):
        a = self.product("A", stock=10)
        with self.assertRaises(AppError) as cm:
            services.create_order(self.conn, [(a["id"], 1), (999, 1)])
        self.assertEqual((cm.exception.status_code, cm.exception.code), (404, "PRODUCT_NOT_FOUND"))
        self.assertEqual(services.get_stock(self.conn, a["id"])["stock"], 10)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 0)

    def test_order_is_all_or_nothing_on_insufficient_stock(self):
        a = self.product("A", stock=10)
        b = self.product("B", stock=1)
        with self.assertRaises(AppError) as cm:
            services.create_order(self.conn, [(a["id"], 5), (b["id"], 2)])
        err = cm.exception
        self.assertEqual((err.status_code, err.code), (409, "INSUFFICIENT_STOCK"))
        self.assertEqual(err.details[0]["product_id"], b["id"])
        self.assertEqual((err.details[0]["requested"], err.details[0]["available"]), (2, 1))
        self.assertEqual(services.get_stock(self.conn, a["id"])["stock"], 10)  # untouched
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 0)

    def test_order_price_is_snapshotted(self):
        a = self.product("A", "10.00", 10)
        order = services.create_order(self.conn, [(a["id"], 1)])
        self.conn.execute("UPDATE products SET price_cents = 9999 WHERE id = ?", (a["id"],))
        self.assertEqual(services.get_order(self.conn, order["id"])["total"], "10.00")

    def test_get_missing_order_is_404(self):
        with self.assertRaises(AppError) as cm:
            services.get_order(self.conn, 123)
        self.assertEqual((cm.exception.status_code, cm.exception.code), (404, "ORDER_NOT_FOUND"))

    def test_concurrent_orders_never_oversell(self):
        p = self.product("HOT", "1.00", 10)
        results = []

        def buy():
            conn = connect(self.path)
            try:
                services.create_order(conn, [(p["id"], 1)])
                results.append("ok")
            except AppError as exc:
                results.append(exc.code)
            finally:
                conn.close()

        threads = [threading.Thread(target=buy) for _ in range(25)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(results.count("ok"), 10)
        self.assertEqual(results.count("INSUFFICIENT_STOCK"), 15)
        self.assertEqual(services.get_stock(self.conn, p["id"])["stock"], 0)


if __name__ == "__main__":
    unittest.main()
