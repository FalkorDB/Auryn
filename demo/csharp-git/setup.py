"""Builds the sample git repository this demo syncs against: a small C# "Widgets"
project with three commits showing realistic evolution --

  1. Core domain model and service layer (Widget, WidgetRepository, WidgetService)
     -- no API layer yet, so nothing is a DTO.
  2. A controller is added, returning the raw `Widget` entity directly. `Widget`
     flips from `Class` to `DTO` purely because of this -- even though
     Entities.cs (where Widget is declared) isn't touched by this commit at all.
  3. Dedicated request/response DTOs are introduced and the controller switches to
     them instead of leaking the domain entity. `Widget` flips *back* to a plain
     `Class` -- again without Entities.cs being touched -- while the two new DTO
     classes take over the `DTO` label.

Usage:
    uv run python demo/csharp-git/setup.py

Safe to re-run: deletes and rebuilds the repo from scratch each time.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import git

REPO_DIR = Path(__file__).parent / "sample-repo"

_ENTITIES_CS = """using System;

namespace Acme.Widgets.Models
{
    public abstract class BaseEntity
    {
        public int Id { get; set; }
        public DateTime CreatedAt { get; set; }
    }

    public class Widget : BaseEntity
    {
        public string Name { get; set; }
        public decimal Price { get; set; }
        public WidgetStatus Status { get; set; }
    }

    public enum WidgetStatus : byte
    {
        Active = 1,
        Retired = 2,
    }

    public readonly record struct Money(decimal Amount, string Currency);

    public delegate void WidgetChangedHandler(Widget widget, WidgetStatus newStatus);
}
"""

_WIDGET_SERVICE_CS = """using System.Collections.Generic;
using Acme.Widgets.Models;

namespace Acme.Widgets.Services
{
    public interface IWidgetRepository
    {
        Widget FindById(int id);
        void Save(Widget widget);
    }

    public class WidgetRepository : IWidgetRepository
    {
        private readonly Dictionary<int, Widget> _widgets = new Dictionary<int, Widget>();
        private int _nextId = 1;

        public Widget FindById(int id)
        {
            return _widgets[id];
        }

        public void Save(Widget widget)
        {
            if (widget.Id == 0)
            {
                widget.Id = _nextId++;
            }
            _widgets[widget.Id] = widget;
        }
    }

    public interface IWidgetService
    {
        Widget GetById(int id);
        Widget Create(string name, decimal price);
    }

    public class WidgetService : IWidgetService
    {
        private readonly IWidgetRepository _repository;

        public WidgetService(IWidgetRepository repository)
        {
            _repository = repository;
        }

        public Widget GetById(int id)
        {
            return _repository.FindById(id);
        }

        public Widget Create(string name, decimal price)
        {
            var widget = new Widget { Name = name, Price = price, Status = WidgetStatus.Active };
            _repository.Save(widget);
            return widget;
        }
    }
}
"""

_CONTROLLER_V1_CS = """using Acme.Widgets.Models;
using Acme.Widgets.Services;
using Microsoft.AspNetCore.Mvc;

namespace Acme.Widgets.Controllers
{
    [ApiController]
    [Route("api/[controller]")]
    public class WidgetsController : ControllerBase
    {
        private readonly IWidgetService _service;

        public WidgetsController(IWidgetService service)
        {
            _service = service;
        }

        [HttpGet("{id}")]
        public Widget GetById(int id)
        {
            return _service.GetById(id);
        }

        [HttpPost]
        public Widget Create(string name, decimal price)
        {
            return _service.Create(name, price);
        }
    }
}
"""

_DTOS_CS = """namespace Acme.Widgets.Models
{
    public class WidgetDto
    {
        public int Id { get; set; }
        public string Name { get; set; }
        public decimal Price { get; set; }
    }

    public class CreateWidgetRequest
    {
        public string Name { get; set; }
        public decimal Price { get; set; }
    }
}
"""

_CONTROLLER_V2_CS = """using Acme.Widgets.Models;
using Acme.Widgets.Services;
using Microsoft.AspNetCore.Mvc;

namespace Acme.Widgets.Controllers
{
    [ApiController]
    [Route("api/[controller]")]
    public class WidgetsController : ControllerBase
    {
        private readonly IWidgetService _service;

        public WidgetsController(IWidgetService service)
        {
            _service = service;
        }

        [HttpGet("{id}")]
        public WidgetDto GetById(int id)
        {
            var widget = _service.GetById(id);
            return new WidgetDto { Id = widget.Id, Name = widget.Name, Price = widget.Price };
        }

        [HttpPost]
        public WidgetDto Create([FromBody] CreateWidgetRequest request)
        {
            var widget = _service.Create(request.Name, request.Price);
            return new WidgetDto { Id = widget.Id, Name = widget.Name, Price = widget.Price };
        }
    }
}
"""


def build_repo() -> Path:
    if REPO_DIR.exists():
        shutil.rmtree(REPO_DIR)
    (REPO_DIR / "src" / "Models").mkdir(parents=True)
    (REPO_DIR / "src" / "Services").mkdir(parents=True)
    (REPO_DIR / "src" / "Controllers").mkdir(parents=True)

    repo = git.Repo.init(str(REPO_DIR), initial_branch="main")
    with repo.config_writer() as cw:
        cw.set_value("user", "email", "demo@example.com")
        cw.set_value("user", "name", "Demo")

    (REPO_DIR / "src" / "Models" / "Entities.cs").write_text(_ENTITIES_CS)
    (REPO_DIR / "src" / "Services" / "WidgetService.cs").write_text(_WIDGET_SERVICE_CS)
    repo.index.add(["src/Models/Entities.cs", "src/Services/WidgetService.cs"])
    repo.index.commit("Core domain model and service layer")
    repo.create_tag("v1-core-domain")

    (REPO_DIR / "src" / "Controllers" / "WidgetsController.cs").write_text(_CONTROLLER_V1_CS)
    repo.index.add(["src/Controllers/WidgetsController.cs"])
    repo.index.commit("Expose Widgets over HTTP")
    repo.create_tag("v2-http-api")

    (REPO_DIR / "src" / "Models" / "Dtos.cs").write_text(_DTOS_CS)
    (REPO_DIR / "src" / "Controllers" / "WidgetsController.cs").write_text(_CONTROLLER_V2_CS)
    repo.index.add(["src/Models/Dtos.cs", "src/Controllers/WidgetsController.cs"])
    repo.index.commit("Introduce dedicated request/response DTOs")
    repo.create_tag("v3-dedicated-dtos")

    return REPO_DIR


def main() -> None:
    path = build_repo()
    repo = git.Repo(str(path))
    tag_by_commit = {t.commit.hexsha: t.name for t in repo.tags}
    print(f"Built sample repo at {path}\n")
    for commit in reversed(list(repo.iter_commits("main"))):
        tag = tag_by_commit.get(commit.hexsha)
        print(f"  {commit.hexsha[:8]}  {'(' + tag + ')  ' if tag else ''}{commit.message.strip()}")
    print()
    print("Ingest with, e.g.:")
    print(
        f'  uv run auryn git-sync csharp --repo-uri "{path}" --project-name Widgets '
        f'--solution-name Acme --ref v1-core-domain'
    )


if __name__ == "__main__":
    main()
