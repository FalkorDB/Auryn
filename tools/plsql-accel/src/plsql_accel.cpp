// All-C++ PL/SQL extraction accelerator.
//
// Parses a preprocessed PL/SQL source string entirely in C++ (ANTLR C++ runtime + the generated
// parser from the SAME pinned grammar as the vendored Python parser) and runs a C++ visitor that
// extracts exactly what auryn.ingestors.plsql.source_analysis's _DeclarationVisitor +
// _CallsAndTableRefsVisitor extract -- returning only the small RawPlsqlFile result as JSON. The
// huge parse tree never crosses into Python, which is what makes this ~10x rather than ~2.6x (the
// speedy-antlr tree-translation approach). The Python side (source_analysis._build_raw_from_json)
// rebuilds the RawPlsqlFile from this JSON; on any error it falls back to the pure-Python parse, so
// this is a drop-in speedup, never a hard dependency.
//
// This file is the ONE place the extraction logic is duplicated in C++. The plsql test suite asserts
// C++ and Python produce byte-identical RawPlsqlFile, so drift is caught immediately.

#define PY_SSIZE_T_CLEAN
#include <Python.h>

#include <algorithm>
#include <cstdio>
#include <string>
#include <vector>

// json.hpp before the ANTLR headers: antlr4's Recognizer defines a class member `EOF`, which
// shadows the <cstdio> EOF *macro* that json.hpp relies on if the ANTLR headers come first.
#include "json.hpp"

#include "antlr4-runtime.h"
#include "PlSqlLexer.h"
#include "PlSqlParser.h"
#include "PlSqlParserBaseVisitor.h"

using json = nlohmann::json;

namespace {

// -- small helpers mirroring source_analysis.py --------------------------------------

std::string toUpper(std::string s) {
    std::transform(s.begin(), s.end(), s.begin(), [](unsigned char c) { return std::toupper(c); });
    return s;
}

// _last_identifier: segment after the last '.', upper-cased.
std::string lastIdentifier(const std::string &text) {
    auto pos = text.rfind('.');
    return toUpper(pos == std::string::npos ? text : text.substr(pos + 1));
}

// firstOf/present normalise ANTLR accessors that may be a single ptr or a vector, so the port
// doesn't have to track each accessor's cardinality.
template <typename T> T *firstOf(T *p) { return p; }
template <typename T> T *firstOf(const std::vector<T *> &v) { return v.empty() ? nullptr : v.front(); }
template <typename T> bool present(T *p) { return p != nullptr; }
template <typename T> bool present(const std::vector<T *> &v) { return !v.empty(); }

// _text: getText() of the first occurrence, or "".
template <typename A> std::string nodeText(const A &accessor) {
    auto *ctx = firstOf(accessor);
    return ctx ? ctx->getText() : std::string();
}

// _slice: original source between a context's start/stop char indices (code-point correct via the
// CharStream, matching Python's source[start:stop+1]).
std::string sliceSource(antlr4::CharStream *input, antlr4::ParserRuleContext *ctx) {
    if (!ctx || !ctx->getStart() || !ctx->getStop()) return std::string();
    return input->getText(antlr4::misc::Interval(ctx->getStart()->getStartIndex(),
                                                 ctx->getStop()->getStopIndex()));
}

// _classify_table_ref_context: nearest ancestor rule that says how a table is used.
const char *classifyTableRef(antlr4::tree::ParseTree *node) {
    for (auto *p = node->parent; p != nullptr; p = p->parent) {
        auto *rc = dynamic_cast<antlr4::RuleContext *>(p);
        if (!rc) continue;
        size_t r = rc->getRuleIndex();
        if (r == PlSqlParser::RuleUpdate_statement) return "UPDATE";
        if (r == PlSqlParser::RuleDelete_statement) return "DELETE";
        if (r == PlSqlParser::RuleSingle_table_insert || r == PlSqlParser::RuleInsert_into_clause)
            return "INSERT";
        if (r == PlSqlParser::RuleFrom_clause) return "SELECT";
    }
    return nullptr;
}

// -- calls + table refs (the _CallsAndTableRefsVisitor) ------------------------------

class EdgeCollector : public PlSqlParserBaseVisitor {
  public:
    json calls = json::array();
    json table_refs = json::array();

    std::any visitCall_statement(PlSqlParser::Call_statementContext *ctx) override {
        auto names = ctx->routine_name();
        if (!names.empty()) calls.push_back(lastIdentifier(names.back()->getText()));
        return visitChildren(ctx);
    }
    std::any visitTableview_name(PlSqlParser::Tableview_nameContext *ctx) override {
        const char *op = classifyTableRef(ctx);
        if (op) table_refs.push_back(json::array({lastIdentifier(ctx->getText()), op}));
        return visitChildren(ctx);
    }
};

void collectEdges(antlr4::tree::ParseTree *ctx, json &calls, json &refs) {
    EdgeCollector c;
    if (ctx) c.visit(ctx);
    calls = c.calls;
    refs = c.table_refs;
}

// -- declaration extraction (the _DeclarationVisitor) -------------------------------

class Extractor : public PlSqlParserBaseVisitor {
  public:
    explicit Extractor(antlr4::CharStream *input) : input_(input) {
        result_ = {{"packages", json::array()},   {"routines", json::array()},
                   {"triggers", json::array()},    {"types", json::array()},
                   {"tables", json::array()},      {"views", json::array()},
                   {"indexes", json::array()},     {"sequences", json::array()},
                   {"alterations", json::array()}, {"statement_kinds", json::array()}};
    }
    json &result() { return result_; }

    // Packages ----------------------------------------------------------------------
    std::any visitCreate_package(PlSqlParser::Create_packageContext *ctx) override {
        json members = json::array();
        for (auto *obj : ctx->package_obj_spec()) {
            if (auto *p = obj->procedure_spec()) members.push_back(routineFromProcSpec(p));
            else if (auto *f = obj->function_spec()) members.push_back(routineFromFuncSpec(f));
        }
        result_["packages"].push_back({{"name", lastIdentifier(nodeText(ctx->package_name()))},
                                       {"spec_source", sliceSource(input_, ctx)},
                                       {"body_source", nullptr},
                                       {"members", members}});
        return std::any{};  // handled subtree -- do not recurse (matches Python)
    }
    std::any visitCreate_package_body(PlSqlParser::Create_package_bodyContext *ctx) override {
        json members = json::array();
        for (auto *obj : ctx->package_obj_body()) {
            if (auto *p = obj->procedure_body()) members.push_back(routineFromProcBody(p));
            else if (auto *f = obj->function_body()) members.push_back(routineFromFuncBody(f));
        }
        result_["packages"].push_back({{"name", lastIdentifier(nodeText(ctx->package_name()))},
                                       {"spec_source", nullptr},
                                       {"body_source", sliceSource(input_, ctx)},
                                       {"members", members}});
        return std::any{};
    }

    // Standalone routines -------------------------------------------------------------
    std::any visitCreate_procedure_body(PlSqlParser::Create_procedure_bodyContext *ctx) override {
        json r = {{"name", lastIdentifier(nodeText(ctx->procedure_name()))},
                  {"kind", "PROCEDURE"},
                  {"parameters", paramsOf(ctx->parameter())},
                  {"return_type", nullptr},
                  {"source", sliceSource(input_, ctx)}};
        edges(ctx, r);
        result_["routines"].push_back(r);
        return std::any{};
    }
    std::any visitCreate_function_body(PlSqlParser::Create_function_bodyContext *ctx) override {
        json r = {{"name", lastIdentifier(nodeText(ctx->function_name()))},
                  {"kind", "FUNCTION"},
                  {"parameters", paramsOf(ctx->parameter())},
                  {"return_type", typeSpecText(ctx->type_spec())},
                  {"source", sliceSource(input_, ctx)}};
        edges(ctx, r);
        result_["routines"].push_back(r);
        return std::any{};
    }

    // Triggers ------------------------------------------------------------------------
    std::any visitCreate_trigger(PlSqlParser::Create_triggerContext *ctx) override {
        json r = {{"name", lastIdentifier(nodeText(ctx->trigger_name()))},
                  {"table", triggerTable(ctx)},
                  {"source", sliceSource(input_, ctx)}};
        json calls = json::array(), refs = json::array();
        collectEdges(ctx->trigger_body(), calls, refs);
        r["calls"] = calls;
        r["table_refs"] = refs;
        result_["triggers"].push_back(r);
        return std::any{};
    }

    // Types ---------------------------------------------------------------------------
    std::any visitCreate_type(PlSqlParser::Create_typeContext *ctx) override {
        auto *def = ctx->type_definition();
        if (!def || !def->type_name()) return std::any{};  // bare TYPE BODY
        json kind = def->object_type_def() ? json("OBJECT") : json(nullptr);
        result_["types"].push_back(
            {{"name", lastIdentifier(nodeText(def->type_name()))}, {"kind", kind}});
        return std::any{};
    }

    // SQL DDL -------------------------------------------------------------------------
    std::any visitCreate_table(PlSqlParser::Create_tableContext *ctx) override {
        auto *relational = ctx->relational_table();
        if (!relational) return std::any{};  // object/xmltype tables -- not modeled
        json columns = json::array(), constraints = json::array(), fks = json::array();
        std::vector<std::string> pkCols;
        std::string pkName;
        int position = 0;
        for (auto *prop : relational->relational_property()) {
            if (auto *cd = prop->column_definition()) {
                columns.push_back(column(cd, ++position, constraints, fks, pkCols, pkName));
            } else if (auto *ool = prop->out_of_line_constraint()) {
                outOfLineConstraint(ool, constraints, fks, pkCols, pkName);
            }
        }
        json table = {{"name", lastIdentifier(nodeText(ctx->table_name()))}};
        finishTable(table, columns, constraints, fks, pkCols, pkName);
        result_["tables"].push_back(table);
        return std::any{};
    }

    std::any visitAlter_table(PlSqlParser::Alter_tableContext *ctx) override {
        json columns = json::array(), constraints = json::array(), fks = json::array();
        std::vector<std::string> pkCols;
        std::string pkName;
        if (auto *cc = ctx->constraint_clauses())
            for (auto *ool : cc->out_of_line_constraint())
                outOfLineConstraint(ool, constraints, fks, pkCols, pkName);
        int position = 0;
        if (auto *colClauses = ctx->column_clauses())
            if (auto *amd = colClauses->add_modify_drop_column_clauses())
                for (auto *addClause : amd->add_column_clause())
                    for (auto *cd : addClause->column_definition())
                        columns.push_back(column(cd, ++position, constraints, fks, pkCols, pkName));

        // Prepend the merged PK constraint + mark its columns (matches CREATE TABLE).
        markPrimaryKey(columns, constraints, pkCols, pkName);
        if (!columns.empty() || !constraints.empty() || !fks.empty()) {
            result_["alterations"].push_back({{"table", lastIdentifier(nodeText(ctx->tableview_name()))},
                                              {"columns", columns},
                                              {"constraints", constraints},
                                              {"foreign_keys", fks}});
        }
        return std::any{};
    }

    std::any visitCreate_view(PlSqlParser::Create_viewContext *ctx) override {
        json reads = json::array();
        if (auto *select = ctx->select_only_statement()) {
            json calls = json::array(), refs = json::array();
            collectEdges(select, calls, refs);
            std::vector<std::string> seen;
            for (auto &ref : refs) {
                std::string n = ref[0];
                if (std::find(seen.begin(), seen.end(), n) == seen.end()) {
                    seen.push_back(n);
                    reads.push_back(n);
                }
            }
        }
        result_["views"].push_back({{"name", lastIdentifier(nodeText(ctx->id_expression()))},
                                    {"definition", sliceSource(input_, ctx)},
                                    {"reads", reads}});
        return std::any{};
    }

    std::any visitCreate_index(PlSqlParser::Create_indexContext *ctx) override {
        auto *clause = ctx->table_index_clause();
        if (!clause) return std::any{};  // cluster/bitmap-join index
        json cols = json::array();
        for (auto *e : clause->index_expr()) cols.push_back(lastIdentifier(e->getText()));
        result_["indexes"].push_back({{"name", lastIdentifier(nodeText(ctx->index_name()))},
                                      {"table", lastIdentifier(nodeText(clause->tableview_name()))},
                                      {"columns", cols},
                                      {"unique", present(ctx->UNIQUE())}});
        return std::any{};
    }

    std::any visitCreate_sequence(PlSqlParser::Create_sequenceContext *ctx) override {
        result_["sequences"].push_back({{"name", lastIdentifier(nodeText(ctx->sequence_name()))}});
        return std::any{};
    }

    // statement_kinds: top-level unit_statement child rule names ("...Context" stripped).
    json statementKinds(PlSqlParser::Sql_scriptContext *tree, const std::vector<std::string> &ruleNames) {
        json kinds = json::array();
        for (auto *child : tree->children) {
            auto *unit = dynamic_cast<PlSqlParser::Unit_statementContext *>(child);
            if (!unit || unit->children.empty()) continue;
            auto *inner = dynamic_cast<antlr4::RuleContext *>(unit->children.front());
            if (inner) kinds.push_back(ruleKind(inner->getRuleIndex(), ruleNames));
        }
        return kinds;
    }

  private:
    antlr4::CharStream *input_;
    json result_;

    void edges(antlr4::tree::ParseTree *ctx, json &routine) {
        json calls = json::array(), refs = json::array();
        collectEdges(ctx, calls, refs);
        routine["calls"] = calls;
        routine["table_refs"] = refs;
    }

    template <typename ParamVec> json paramsOf(const ParamVec &params) {
        json arr = json::array();
        int position = 0;
        for (auto *p : params) {
            arr.push_back({{"name", present(p->parameter_name())
                                        ? json(firstOf(p->parameter_name())->getText())
                                        : json(nullptr)},
                           {"position", ++position},
                           {"direction", direction(p)},
                           {"data_type", present(p->type_spec())
                                             ? json(firstOf(p->type_spec())->getText())
                                             : json(nullptr)},
                           {"has_default", present(p->default_value_part())}});
        }
        return arr;
    }

    template <typename P> json direction(P *p) {
        bool hasIn = present(p->IN()), hasOut = present(p->OUT());
        if (hasIn && hasOut) return "IN OUT";
        if (hasOut) return "OUT";
        if (hasIn) return "IN";
        return nullptr;
    }

    template <typename TS> json typeSpecText(TS accessor) {
        auto *ts = firstOf(accessor);
        return ts ? json(ts->getText()) : json(nullptr);
    }

    // return_type is passed in (not read off the ctx) so these templates never reference type_spec on
    // a procedure context, which doesn't have it.
    template <typename SpecCtx> json routineFromSpec(SpecCtx *spec, const char *kind, json returnType) {
        return {{"name", toUpper(nodeText(spec->identifier()))},
                {"kind", kind},
                {"parameters", paramsOf(spec->parameter())},
                {"return_type", returnType},
                {"source", nullptr},
                {"calls", json::array()},
                {"table_refs", json::array()}};
    }
    json routineFromProcSpec(PlSqlParser::Procedure_specContext *s) {
        return routineFromSpec(s, "PROCEDURE", nullptr);
    }
    json routineFromFuncSpec(PlSqlParser::Function_specContext *s) {
        return routineFromSpec(s, "FUNCTION", typeSpecText(s->type_spec()));
    }

    template <typename BodyCtx> json routineFromBody(BodyCtx *body, const char *kind, json returnType) {
        json r = {{"name", toUpper(nodeText(body->identifier()))},
                  {"kind", kind},
                  {"parameters", paramsOf(body->parameter())},
                  {"return_type", returnType},
                  {"source", sliceSource(input_, body)}};
        edges(body, r);
        return r;
    }
    json routineFromProcBody(PlSqlParser::Procedure_bodyContext *b) {
        return routineFromBody(b, "PROCEDURE", nullptr);
    }
    json routineFromFuncBody(PlSqlParser::Function_bodyContext *b) {
        return routineFromBody(b, "FUNCTION", typeSpecText(b->type_spec()));
    }

    template <typename TriggerCtx> json triggerTable(TriggerCtx *ctx) {
        auto *simple = ctx->simple_dml_trigger();
        if (!simple) return nullptr;
        auto *event = simple->dml_event_clause();
        if (!event || !firstOf(event->tableview_name())) return nullptr;
        return lastIdentifier(nodeText(event->tableview_name()));
    }

    // A single column_definition + its inline constraints.
    json column(PlSqlParser::Column_definitionContext *ctx, int position, json &constraints, json &fks,
                std::vector<std::string> &pkCols, std::string &pkName) {
        std::string name = lastIdentifier(nodeText(ctx->column_name()));
        auto *dt = firstOf(ctx->datatype());
        json dataType = dt ? json(dt->getText()) : (present(ctx->type_name())
                                                        ? json(firstOf(ctx->type_name())->getText())
                                                        : json(nullptr));
        bool notNull = false;
        for (auto *inl : ctx->inline_constraint()) {
            if (present(inl->PRIMARY())) {
                pkCols.push_back(name);
                if (pkName.empty()) pkName = constraintName(inl);
            }
            if (present(inl->NOT()) && present(inl->NULL_())) notNull = true;
            if (present(inl->UNIQUE()))
                constraints.push_back(constraintJson(constraintName(inl), "UNIQUE", {name}, nullptr));
            if (auto *check = inl->check_constraint())
                constraints.push_back(constraintJson(constraintName(inl), "CHECK", {},
                                                     check->condition()->getText()));
            if (auto *ref = inl->references_clause())
                fks.push_back(foreignKey(constraintName(inl), {name}, ref));
        }
        auto *expr = firstOf(ctx->expression());
        return {{"name", name},
                {"position", position},
                {"data_type", dataType},
                {"nullable", !notNull},
                {"default_value", expr ? json(expr->getText()) : json(nullptr)},
                {"is_primary_key", false}};
    }

    void outOfLineConstraint(PlSqlParser::Out_of_line_constraintContext *ctx, json &constraints,
                             json &fks, std::vector<std::string> &pkCols, std::string &pkName) {
        std::string name = constraintName(ctx);
        std::vector<std::string> columns;
        for (auto *c : ctx->column_name()) columns.push_back(lastIdentifier(c->getText()));
        if (auto *fk = ctx->foreign_key_clause()) {
            std::vector<std::string> constrained;
            for (auto *c : columnNames(fk->paren_column_list()))
                constrained.push_back(lastIdentifier(c->getText()));
            fks.push_back(foreignKey(name, constrained, fk->references_clause()));
        } else if (present(ctx->PRIMARY())) {
            pkCols.insert(pkCols.end(), columns.begin(), columns.end());
            if (pkName.empty()) pkName = name;
        } else if (present(ctx->UNIQUE())) {
            constraints.push_back(constraintJson(name, "UNIQUE", columns, nullptr));
        } else if (present(ctx->CHECK()) && ctx->condition()) {
            constraints.push_back(constraintJson(name, "CHECK", {}, ctx->condition()->getText()));
        }
    }

    json foreignKey(const std::string &name, const std::vector<std::string> &columns,
                    PlSqlParser::References_clauseContext *ref) {
        std::vector<std::string> refCols;
        for (auto *c : columnNames(ref->paren_column_list()))
            refCols.push_back(lastIdentifier(c->getText()));
        return {{"name", name.empty() ? json(nullptr) : json(name)},
                {"columns", columns},
                {"ref_table", lastIdentifier(nodeText(ref->tableview_name()))},
                {"ref_columns", refCols}};
    }

    std::vector<PlSqlParser::Column_nameContext *> columnNames(
        PlSqlParser::Paren_column_listContext *pcl) {
        if (!pcl || !pcl->column_list()) return {};
        return pcl->column_list()->column_name();
    }

    std::string constraintName(PlSqlParser::Inline_constraintContext *ctx) {
        return ctx->constraint_name() ? lastIdentifier(ctx->constraint_name()->getText()) : std::string();
    }
    std::string constraintName(PlSqlParser::Out_of_line_constraintContext *ctx) {
        return ctx->constraint_name() ? lastIdentifier(ctx->constraint_name()->getText()) : std::string();
    }

    json constraintJson(const std::string &name, const char *type,
                        const std::vector<std::string> &columns, json expression) {
        return {{"name", name.empty() ? json(nullptr) : json(name)},
                {"type", type},
                {"columns", columns},
                {"expression", expression}};
    }

    void markPrimaryKey(json &columns, json &constraints, const std::vector<std::string> &pkCols,
                        const std::string &pkName) {
        if (pkCols.empty()) return;
        std::vector<std::string> upper;
        for (auto &c : pkCols) upper.push_back(toUpper(c));
        for (auto &col : columns)
            if (std::find(upper.begin(), upper.end(), toUpper(col["name"].get<std::string>())) !=
                upper.end())
                col["is_primary_key"] = true;
        constraints.insert(constraints.begin(),
                           constraintJson(pkName, "PRIMARY_KEY", pkCols, nullptr));
    }

    void finishTable(json &table, json &columns, json &constraints, json &fks,
                     const std::vector<std::string> &pkCols, const std::string &pkName) {
        markPrimaryKey(columns, constraints, pkCols, pkName);
        table["columns"] = columns;
        table["constraints"] = constraints;
        table["foreign_keys"] = fks;
    }

    std::string ruleKind(size_t ruleIndex, const std::vector<std::string> &ruleNames) {
        std::string name = ruleIndex < ruleNames.size() ? ruleNames[ruleIndex] : "";  // "create_table"
        if (!name.empty()) name[0] = std::toupper((unsigned char)name[0]);
        return name;  // "Create_table"
    }
};

// -- module entry -------------------------------------------------------------------

class CollectingErrorListener : public antlr4::BaseErrorListener {
  public:
    json errors = json::array();
    void syntaxError(antlr4::Recognizer *, antlr4::Token *, size_t line, size_t col,
                     const std::string &, std::exception_ptr) override {
        errors.push_back(json::array({(int)line, (int)col}));
    }
};

PyObject *extract_impl(PyObject *, PyObject *args) {
    const char *src = nullptr;
    Py_ssize_t len = 0;
    if (!PyArg_ParseTuple(args, "s#", &src, &len)) return nullptr;
    std::string source(src, (size_t)len);
    std::string out;
    Py_BEGIN_ALLOW_THREADS
    try {
        antlr4::ANTLRInputStream input(source);
        PlSqlLexer lexer(&input);
        lexer.removeErrorListeners();
        antlr4::CommonTokenStream tokens(&lexer);
        PlSqlParser parser(&tokens);
        parser.removeErrorListeners();
        CollectingErrorListener errs;
        parser.addErrorListener(&errs);  // default error *recovery* -> partial tree + recorded errors
        auto *tree = parser.sql_script();
        Extractor ex(&input);
        ex.visit(tree);
        ex.result()["statement_kinds"] = ex.statementKinds(tree, parser.getRuleNames());
        ex.result()["errors"] = errs.errors;
        out = ex.result().dump();
    } catch (const std::exception &e) {
        out = std::string("\x01") + e.what();  // sentinel -> Python raises + falls back
    }
    Py_END_ALLOW_THREADS
    if (!out.empty() && out[0] == '\x01') {
        PyErr_SetString(PyExc_RuntimeError, out.c_str() + 1);
        return nullptr;
    }
    return PyUnicode_FromStringAndSize(out.data(), (Py_ssize_t)out.size());
}

PyMethodDef methods[] = {
    {"extract", extract_impl, METH_VARARGS, "Parse PL/SQL source and return the RawPlsqlFile as JSON."},
    {nullptr, nullptr, 0, nullptr}};

PyModuleDef module = {PyModuleDef_HEAD_INIT, "plsql_accel", "C++ PL/SQL extraction accelerator", -1,
                      methods};

}  // namespace

PyMODINIT_FUNC PyInit_plsql_accel(void) { return PyModule_Create(&module); }
